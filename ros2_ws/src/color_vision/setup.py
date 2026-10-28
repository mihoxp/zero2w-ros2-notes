from setuptools import setup

package_name = "color_vision"

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
    description="OV5647 camera colour detection (traffic light state, red/yellow/green objects)",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "color_detector = color_vision.color_detector:main",
        ],
    },
)
