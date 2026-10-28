from setuptools import setup

package_name = "v7rc_bridge"

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
    maintainer="ws",
    maintainer_email="ws@example.com",
    description="V7RC app (Wi-Fi UDP, car mode) to /cmd_vel bridge with failsafe stop",
    license="Apache-2.0",
    entry_points={
        "console_scripts": [
            "v7rc_bridge = v7rc_bridge.v7rc_bridge:main",
        ],
    },
)
