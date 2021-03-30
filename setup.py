from setuptools import setup

setup(
    name='qmk-hermit',
    version='0.0.1',
    py_modules=['qmk_hermit'],
    entry_points='''
        [console_scripts]
        qmk-hermit=qmk_hermit:main
    ''',
)
