# Simple Launch

`simple-launch` is a simple tool to launch and manage multiple processes. It is based on the process management code
extracted from the `pyri-core` module, part of the PyRI Open-Source Teach Pendant

`simple-launch` has a similar goal to `roslaunch`, but
designed to be simpler and independent of ROS. It also has improvements in handling Windows process management
using Windows Job Objects. Yaml files are used to configure the processes to launch.

`simple-launch` will show a minimal GUI windows when the `--gui` is specified. This window shows same basic
information, and has a "Stop All" button that will shut down all tasks. This GUI is useful if `simple-launch` is 
started interactively by the user rather than running as a background service.

See also `simple-launch-process`, a companion library that contains utility functions to use within processes
hosted by `simple-launch`. The functions `wait_exit()` and `wait_exit_callback()` assist in receiving shutdown
signals in a reliable cross-platform manner.

See `simple-launch-process`: https://github.com/johnwason/simple-launch-process

## Installation

```
python -m pip install --user simple-launch
```

On Ubuntu, it may be necessary to replace `python` with `python3`.

## Usage

Yaml files are used to configure the processes to launch. The following is an example of a `simple-launch.yaml` 
file that launches two Python servers.

```yaml
name: example_http_servers
tasks:
  - name: http_server_1
    program: python.exe
    args: example_http_server.py 8100 Server1
    cwd: .
  - name: http_server_2
    program: python.exe
    args:
      - example_http_server.py
      - 8101
      - Hello from Server2!
    start-delay: 1
```

The `example_http_server.py` program uses `simple-launch-process` to wait for the shutdown signal.

In this example, the `name` field is the name of the launch group. The `tasks` section contains a list of tasks to 
launch. The following fields are used to configure the launch of each task:

* `name`: The name of the process
* `program`: The program to execute
* `args`: The command line arguments to pass to `program`
* `cwd` (optional): The working directory to run the program. Defaults to current directory.
* `start-delay` (optional): Delay the start of the program by specified seconds
* `restart` (optional): If true, process will be restarted if it exits
* `restart-backoff` (optional): Delay in seconds before restarting after exit
* `quit-on-terminate` (optional): If `true`, all processes will exit if the process exits
* `tags` (optional): Unused
* `environment` (optional): Key/value pairs to add to environment
* `env-file` (optional): File of environmental variable to override the environment

`simple-launch` by default will load `simple-launch.yaml` in the current directory. To run with default settings,
simply call `simple-launch`.

```
simple-launch
```

In some cases, it may be necessary to use `python` to launch. The `simple-launch` executable may not be installed
to `PATH`.

```
python -m simple_launch
```

`simple-launch` accepts the following optional command line arguments:

* `--config=`: Specify an alternative configuration file
* `--config-js=`: Specify an alternative configuration file that uses Jinja2 templates
* `--cwd=`: Set the default working directory for processes
* `--name=`: Override the name of the launch group
* `--quiet`: Flag to suppress outputting to the terminal
* `--gui`: Show a simple GUI to stop the tasks

Simple Launch saves the output to log files located in the following locations:

* Windows: `%LOCALAPPDATA%\simple-launch\simple-launch\Logs`
* Linux: `$HOME/.cache/SuperApp/log`
* Mac OS: `$HOME//Library/Logs/simple-launch`

Logs should be periodically cleaned if too much disk space is used.

*TODO: Add automatic log cleaning.*

Jinja2 Templates can be used to generate configurations that fill in values at runtime or even include other
templates. See the Jinja2 documentation (https://jinja.palletsprojects.com/) for more information on using templates.

Four variables are available in the template:

* `configpath`: Absolute path to the configuration file
* `configdir`: Directory containing the configuration file
* `env`: Environmental variables
* `platform`: Current platform of the system as returned by `sys.platform`. Typically `win32`, `linux`, or `darwin` (Mac OS)
* `vars`: Extra variables passed on the command line. Use `--var-name=value`, where `name` and `value` are replaced with specific values.

For example, if `CONDA_PREFIX` needs to be used:

```yaml
name: example_http_servers
tasks:
  - name: http_server_1
    program: {{ env["CONDA_PREFIX"] }}/python.exe
    args: {{ configdir }}/example_http_server.py 8100 Server1
    cwd: {{ env["CONDA_PREFIX"] }}
```


## License

Apache 2.0
