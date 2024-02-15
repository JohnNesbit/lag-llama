
from setuptools import setup

setup(
    name='llamaLib',
    url='https://github.com/JohnNesbit/lag-llama/',
    author='John Nesbit',
    author_email='johnmnesbit@gmail.com',
    packages=['llamaLib', 'llamaLib.data','llamaLib.data.augmentations','llamaLib.gluon_utils.gluon_ts_distributions', 'llamaLib.gluon_utils.scalers', 'llamaLib.lag_llama.gluon', 'llamaLib.lag_llama.model', 'llamaLib.gluon_utils', 'llamaLib.lag_llama'],
    install_requires=['numpy'],
    version='0.1',
    license='MIT',
    description='LagLlamma packaged',
)
