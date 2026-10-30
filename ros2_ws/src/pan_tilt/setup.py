from setuptools import setup

package_name = "pan_tilt"

setup(
    name=package_name,
    version="0.1.0",
    packages=[package_name],
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml"]),
    ],
    install_requires=["setuptools"],
    zip_safe=True,
    maintainer="pi",
    maintainer_email="pi@example.com",
    description="Camera pan-tilt on a SparkFun Servo pHAT (PCA9685)",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "pan_tilt_node = pan_tilt.pan_tilt_node:main",
        ],
    },
)
