from typing import Optional, Iterable, Dict, Any

import torch
import pytorch_lightning as pl

from gluonts.core.component import validated
from gluonts.dataset.common import Dataset
from gluonts.dataset.field_names import FieldName
from gluonts.dataset.loader import as_stacked_batches
from gluonts.dataset.stat import calculate_dataset_statistics
from gluonts.time_feature import (
    get_lags_for_frequency,
    TimeFeature,
    time_features_from_frequency_str,
)
from gluonts.itertools import Cyclic
import torch
from pydantic import BaseModel

class DistributionLoss(BaseModel):
    """
    A ``torch.nn.Module`` extensions that computes loss values by comparing a
    ``Distribution`` (prediction) to a ``Tensor`` (ground-truth).
    """

    def __call__(
        self, input: torch.distributions.Distribution, target: torch.Tensor
    ) -> torch.Tensor:
        """
        Compute the loss of predicting ``target`` with the ``input``
        distribution.

        Parameters
        ----------
        input
            Distribution object representing the prediction.
        target
            Tensor containing the ground truth.

        Returns
        -------
        torch.Tensor
            Tensor containing loss values, with the same shape as ``target``.

        Raises
        ------
        NotImplementedError
            [description]
        """
        raise NotImplementedError


class NegativeLogLikelihood(DistributionLoss):
    """
    Compute the negative log likelihood loss.

    Parameters
    ----------
    beta: float in range (0, 1)
        beta parameter from the paper: "On the Pitfalls of Heteroscedastic
        Uncertainty Estimation with Probabilistic Neural Networks" by
        Seitzer et al. 2022
        https://openreview.net/forum?id=aPOpXlnV1T
    """

    beta: float = 0.0

    def __call__(
        self, input: torch.distributions.Distribution, target: torch.Tensor
    ) -> torch.Tensor:
        nll = -input.log_prob(target)
        if self.beta > 0.0:
            variance = input.variance
            nll = nll * (variance.detach() ** self.beta)
        return nll


class CRPS(DistributionLoss):
    def __call__(
        self, input: torch.distributions.Distribution, target: torch.Tensor
    ) -> torch.Tensor:
        return input.crps(target)


class QuantileLoss(DistributionLoss):
    def __call__(
        self, input: torch.distributions.Distribution, target: torch.Tensor
    ) -> torch.Tensor:
        return input.quantile_loss(target)


class EnergyScore(DistributionLoss):
    def __call__(self, input, target: torch.Tensor) -> torch.Tensor:
        return input.energy_score(target)
        
from gluonts.transform import (
    Chain,
    Transformation,
    ValidationSplitSampler,
    TestSplitSampler,
    AddObservedValuesIndicator,
    AddTimeFeatures,
    ExpectedNumInstanceSampler,
    DummyValueImputation,
    InstanceSampler,
    InstanceSplitter,
)
from gluonts.torch.model.estimator import PyTorchLightningEstimator
from gluonts.torch.model.predictor import PyTorchPredictor
from gluonts.torch.distributions import DistributionOutput, StudentTOutput

from llamaLib.lightning_module import LagLlamaLightningModule

PREDICTION_INPUT_NAMES = [
    "past_target",
    "past_observed_values",
    "past_time_feat",
    "future_time_feat",
]

TRAINING_INPUT_NAMES = PREDICTION_INPUT_NAMES + [
    "future_target",
    "future_observed_values",
]


class LagLlamaEstimator(PyTorchLightningEstimator):
    """
    An estimator training a ConvTSMixer model for forecasting.

    This class is uses the model defined in ``ConvTSMixerModel``,
    and wraps it into a ``ConvTSMixerLightningModule`` for training
    purposes: training is performed using PyTorch Lightning's ``pl.Trainer``
    class.

    Parameters
    ----------
    prediction_length
        Length of the prediction horizon.
    context_length
        Number of time steps prior to prediction time that the model
        takes as inputs (default: ``10 * prediction_length``).
    lr
        Learning rate (default: ``1e-3``).
    weight_decay
        Weight decay regularization parameter (default: ``1e-8``).
    distr_output
        Distribution to use to evaluate observations and sample predictions
        (default: StudentTOutput()).
    loss
        Loss to be optimized during training
        (default: ``NegativeLogLikelihood()``).
    batch_norm
        Whether to apply batch normalization.
    batch_size
        The size of the batches to be used for training (default: 32).
    num_batches_per_epoch
        Number of batches to be processed in each training epoch
            (default: 50).
    trainer_kwargs
        Additional arguments to provide to ``pl.Trainer`` for construction.
    train_sampler
        Controls the sampling of windows during training.
    validation_sampler
        Controls the sampling of windows during validation.
    """

    @validated()
    def __init__(
        self,
        prediction_length: int,
        context_length: Optional[int] = None,
        input_size: int = 1,
        n_layer: int = 1,
        n_embd: int = 32,
        n_head: int = 4,
        max_context_length: int = 2048,
        rope_scaling=None,
        scaling: Optional[str] = "mean",
        lr: float = 1e-3,
        weight_decay: float = 1e-8,
        aug_prob: float = 0.1,
        aug_rate: float = 0.1,
        distr_output: DistributionOutput = StudentTOutput(),
        loss: DistributionLoss = NegativeLogLikelihood(),
        num_parallel_samples: int = 100,
        batch_size: int = 32,
        num_batches_per_epoch: int = 50,
        trainer_kwargs: Optional[Dict[str, Any]] = None,
        train_sampler: Optional[InstanceSampler] = None,
        validation_sampler: Optional[InstanceSampler] = None,
        ckpt_path: Optional[str] = None,
    ) -> None:
        default_trainer_kwargs = {"max_epochs": 100}
        if trainer_kwargs is not None:
            default_trainer_kwargs.update(trainer_kwargs)
        super().__init__(trainer_kwargs=default_trainer_kwargs)

        self.scaling = scaling
        self.input_size = input_size
        self.prediction_length = prediction_length
        self.context_length = context_length or 10 * prediction_length
        self.max_context_length = max_context_length
        self.lags_seq = sorted(
            list(
                set(
                    #get_lags_for_frequency(freq_str="Q", num_default_lags=1) +
                    get_lags_for_frequency(freq_str="M", num_default_lags=1)
                    + get_lags_for_frequency(freq_str="W", num_default_lags=1)
                    + get_lags_for_frequency(freq_str="D", num_default_lags=1)
                    + get_lags_for_frequency(freq_str="H", num_default_lags=1)
                    + get_lags_for_frequency(freq_str="T", num_default_lags=1)
                    + get_lags_for_frequency(freq_str="S", num_default_lags=1)
                )
            )
        )

        self.n_head = n_head
        self.n_layer = n_layer
        self.n_embd = n_embd
        self.rope_scaling = rope_scaling

        self.lr = lr
        self.weight_decay = weight_decay
        self.distr_output = distr_output
        self.num_parallel_samples = num_parallel_samples
        self.loss = loss
        self.batch_size = batch_size
        self.num_batches_per_epoch = num_batches_per_epoch

        self.train_sampler = train_sampler or ExpectedNumInstanceSampler(
            num_instances=1.0, min_future=prediction_length
        )
        self.validation_sampler = validation_sampler or ValidationSplitSampler(
            min_future=prediction_length
        )

        self.aug_prob = aug_prob
        self.aug_rate = aug_rate

        self.ckpt_path = ckpt_path

    @classmethod
    def derive_auto_fields(cls, train_iter):
        stats = calculate_dataset_statistics(train_iter)

        return {
            "num_feat_dynamic_real": stats.num_feat_dynamic_real,
            "num_feat_static_cat": len(stats.feat_static_cat),
            "cardinality": [len(cats) for cats in stats.feat_static_cat],
        }

    def create_transformation(self) -> Transformation:
        return Chain(
            [
                AddTimeFeatures(
                    start_field=FieldName.START,
                    target_field=FieldName.TARGET,
                    output_field=FieldName.FEAT_TIME,
                    time_features=time_features_from_frequency_str("S"),
                    pred_length=self.prediction_length,
                ),
                # FilterTransformation(lambda x: sum(abs(x[FieldName.TARGET])) > 0),
                AddObservedValuesIndicator(
                    target_field=FieldName.TARGET,
                    output_field=FieldName.OBSERVED_VALUES,
                    imputation_method=DummyValueImputation(0.0),
                ),
            ]
        )

    def create_lightning_module(self) -> pl.LightningModule:
        model_kwargs = {
            "input_size": self.input_size,
            "max_context_length": self.max_context_length,
            "lags_seq": self.lags_seq,
            "n_layer": self.n_layer,
            "n_embd": self.n_embd,
            "n_head": self.n_head,
            "scaling": self.scaling,
            "distr_output": self.distr_output,
            "num_parallel_samples": self.num_parallel_samples,
            "rope_scaling": self.rope_scaling,
        }
        if self.ckpt_path is not None:
            return LagLlamaLightningModule.load_from_checkpoint(
                checkpoint_path=self.ckpt_path,
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                context_length=self.context_length,
                prediction_length=self.prediction_length,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
                model_kwargs=model_kwargs,
            )
        else:
            return LagLlamaLightningModule(
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                context_length=self.context_length,
                prediction_length=self.prediction_length,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
                model_kwargs=model_kwargs,
            )
    def load_from_checkpoint(self, best_model_path) -> pl.LightningModule:
        model_kwargs = {
            "input_size": self.input_size,
            "max_context_length": self.max_context_length,
            "lags_seq": self.lags_seq,
            "n_layer": self.n_layer,
            "n_embd": self.n_embd,
            "n_head": self.n_head,
            "scaling": self.scaling,
            "distr_output": self.distr_output,
            "num_parallel_samples": self.num_parallel_samples,
            "rope_scaling": self.rope_scaling,
        }
        if best_model_path is not None:
            return LagLlamaLightningModule.load_from_checkpoint(
                checkpoint_path=best_model_path,
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                context_length=self.context_length,
                prediction_length=self.prediction_length,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
                model_kwargs=model_kwargs,
            )
        else:
            return LagLlamaLightningModule(
                loss=self.loss,
                lr=self.lr,
                weight_decay=self.weight_decay,
                context_length=self.context_length,
                prediction_length=self.prediction_length,
                aug_prob=self.aug_prob,
                aug_rate=self.aug_rate,
                model_kwargs=model_kwargs,
            )
    def _create_instance_splitter(self, module: LagLlamaLightningModule, mode: str):
        assert mode in ["training", "validation", "test"]

        instance_sampler = {
            "training": self.train_sampler,
            "validation": self.validation_sampler,
            "test": TestSplitSampler(),
        }[mode]

        return InstanceSplitter(
            target_field=FieldName.TARGET,
            is_pad_field=FieldName.IS_PAD,
            start_field=FieldName.START,
            forecast_start_field=FieldName.FORECAST_START,
            instance_sampler=instance_sampler,
            past_length=self.context_length + max(self.lags_seq),
            future_length=self.prediction_length,
            time_series_fields=[FieldName.FEAT_TIME, FieldName.OBSERVED_VALUES],
            dummy_value=self.distr_output.value_in_support,
        )

    def create_training_data_loader(
        self,
        data: Dataset,
        module: LagLlamaLightningModule,
        shuffle_buffer_length: Optional[int] = None,
        **kwargs,
    ) -> Iterable:
        data = Cyclic(data).stream()
        instances = self._create_instance_splitter(module, "training").apply(
            data, is_train=True
        )
        return as_stacked_batches(
            instances,
            batch_size=self.batch_size,
            shuffle_buffer_length=shuffle_buffer_length,
            field_names=TRAINING_INPUT_NAMES,
            output_type=torch.tensor,
            num_batches_per_epoch=self.num_batches_per_epoch,
        )

    def create_validation_data_loader(
        self,
        data: Dataset,
        module: LagLlamaLightningModule,
        **kwargs,
    ) -> Iterable:
        instances = self._create_instance_splitter(module, "validation").apply(
            data, is_train=True
        )
        return as_stacked_batches(
            instances,
            batch_size=self.batch_size,
            field_names=TRAINING_INPUT_NAMES,
            output_type=torch.tensor,
        )

    def create_predictor(
        self,
        transformation: Transformation,
        module,
    ) -> PyTorchPredictor:
        prediction_splitter = self._create_instance_splitter(module, "test")

        return PyTorchPredictor(
            input_transform=transformation + prediction_splitter,
            input_names=PREDICTION_INPUT_NAMES,
            prediction_net=module,
            batch_size=self.batch_size,
            prediction_length=self.prediction_length,
            device="cuda" if torch.cuda.is_available() else "cpu",
        )
