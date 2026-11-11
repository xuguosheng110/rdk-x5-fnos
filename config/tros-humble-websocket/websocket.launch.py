# Copyright (c) 2024，D-Robotics.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import subprocess

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch_ros.actions import Node
from launch.substitutions import LaunchConfiguration
from ament_index_python.packages import get_package_prefix


def generate_launch_description():
    webserver_path = os.path.join(
        get_package_prefix('websocket'), "lib/websocket/webservice")
    nginx = os.path.join(webserver_path, "sbin/nginx")
    # The packaged conf/nginx.conf declares an absolute PID location.
    pid_file = "/tmp/nginx.pid"

    # Match the PID recorded by this nginx instance, not an arbitrary command
    # line containing "./sbin/nginx -p .".  The old ps-substring test could
    # match its parent shell and skip startup while port 8000 was still down.
    nginx_running = False
    try:
        with open(pid_file, encoding="ascii") as pid_handle:
            pid = int(pid_handle.read().strip())
        process_exe = os.readlink(f"/proc/{pid}/exe")
        nginx_running = os.path.samefile(process_exe, nginx)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        nginx_running = False

    if not nginx_running:
        print("launch webserver")
        print("webserver_path is ", webserver_path)
        print("launch webserver executable is ", nginx)
        subprocess.run(
            [nginx, "-p", webserver_path + os.sep],
            cwd=webserver_path,
            check=True)
    else:
        print("webserver has launch")

    return LaunchDescription([
        DeclareLaunchArgument(
            'websocket_image_topic',
            default_value='/image_jpeg',
            description='image subscribe topic name'),
        DeclareLaunchArgument(
            'websocket_image_type',
            default_value='mjpeg',
            description='image type'),
        DeclareLaunchArgument(
            'websocket_only_show_image',
            default_value='False',
            description='only show image'),
        DeclareLaunchArgument(
            'websocket_output_fps',
            default_value='0',
            description='output fps'),
        DeclareLaunchArgument(
            'websocket_smart_topic',
            default_value='/hobot_mono2d_body_detection',
            description='smart message subscribe topic name'),
        DeclareLaunchArgument(
            'websocket_channel',
            default_value='0',
            description='web channel for show stream'),
        DeclareLaunchArgument(
            'log_level',
            default_value='warn',
            description='Log level'),
        Node(
            package='websocket',
            executable='websocket',
            output='screen',
            parameters=[
                {"image_topic": LaunchConfiguration('websocket_image_topic')},
                {"image_type": LaunchConfiguration('websocket_image_type')},
                {"only_show_image": LaunchConfiguration(
                    'websocket_only_show_image')},
                {"output_fps": LaunchConfiguration('websocket_output_fps')},
                {"channel": LaunchConfiguration('websocket_channel')},
                {"smart_topic": LaunchConfiguration('websocket_smart_topic')}
            ],
            arguments=['--ros-args', '--log-level', LaunchConfiguration('log_level')]
        )
    ])
