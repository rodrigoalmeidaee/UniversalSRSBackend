import setuptools


setuptools.setup(
    name="universalsrs",
    version="0.1",
    packages=["universalsrs"],
    setup_requires=["setuptools-git==1.2"],
    install_requires=[
        "pymongo",
        "flask",
        "flask-cors",
    ],
    entry_points={
        "paste.app_factory": "gevent = universalsrs.uwsgi:main",
    },
    include_package_data=True,
    zip_safe=False,
)
