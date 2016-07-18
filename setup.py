from setuptools import setup, find_packages

setup(
    name='vumi-telegram',
    url='https://github.com/praekelt/vumi-telegram',
    license='BSD',
    description='A Telegram transport for Vumi and Junebug',
    long_description=open('README.md', 'r').read(),
    author='Praekelt Foundation',
    author_email='dev@praekeltfoundation.org',
    packages=find_packages(),
    install_requires=[
        'vumi>=0.6.0',
    ],
    classifiers=[
        'Intended Audience :: Developers',
        'License :: OSI Approved :: BSD License',
        'Operating System :: POSIX',
        'Programming Language :: Python',
        'Programming Language :: Python :: 2.7',
        'Topic :: Software Development :: Libraries :: Python Modules',
        'Topic :: System :: Networking',
    ],
)
