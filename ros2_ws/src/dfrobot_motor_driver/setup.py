from setuptools import setup

package_name = "dfrobot_motor_driver"

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
    description="/cmd_vel to DFRobot DC Motor Driver HAT (DFR0592) over I2C",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "motor_driver = dfrobot_motor_driver.motor_driver:main",
        ],
    },
)
