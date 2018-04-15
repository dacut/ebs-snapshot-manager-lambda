from setuptools import setup

setup(
    name="ebs-snapshot-manager-lambda",
    version="0.1",
    py_modules=["ebs_snapshot_manager.py"],
    author="David Cuthbert",
    author_email="dacut@kanga.org",
    description="Automatically handle snapshots for EBS volumes",
    classifiers=[
        "Development Status :: 3 - Alpha",
        "License :: OSI Approved :: Apache Software License",
        "Programming Language :: Python :: 3.6",
        "Topic :: System :: Archiving :: Backup",
        "Environment :: Other Environment",
    ],
    url="https://github.com/dacut/ebs-snapshot-manager-lambda",
)
