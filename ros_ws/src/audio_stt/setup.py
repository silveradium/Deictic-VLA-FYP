from setuptools import setup

package_name = "audio_stt"

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
    entry_points={"console_scripts": [
        "stt_node = audio_stt.stt_node:main",
        "wake_router = audio_stt.wake_router:main",
    ]},
)
