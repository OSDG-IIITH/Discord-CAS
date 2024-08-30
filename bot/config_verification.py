"""
To read configuration files, parse them and validate each configuration.
This module defines the following function:

- `read_and_validate_config()`: read a config file and validate all configs in it
"""

from configparser import ConfigParser
import sys
from typing import TypedDict


class ConfigEntry(TypedDict):
    configname: str
    grantroles: set[str]
    deleteroles: set[str]
    is_academic: bool
    setrealname: bool


def read_and_validate_config(config_file_path: str):
    """
    Take in a `ConfigParser` object along with the path to a config file.
    Read the file into the parser and then validate each configuration in it,
    by checking for unknown or missing keys.

    Parameters:
    - `config_file_path`: a string, the path to the file containing server configurations

    Returns:
    - A dict mapping from serverid (int) to a ConfigEntry dict (which stores all config
      info). If there is an error, it is printed to stdout, and None is returned.
    """
    server_config = ConfigParser()
    server_config.read(config_file_path, encoding="utf-8")

    ret: dict[int, ConfigEntry] = {}
    for section in server_config.sections():
        section_obj = server_config[section]
        try:
            cur = ret[section_obj.getint("serverid")] = {
                "configname": section,
                "grantroles": set(
                    i for i in section_obj.get("grantroles").split(",") if i
                ),
                "deleteroles": set(
                    i for i in section_obj.get("deleteroles").split(",") if i
                ),
                "is_academic": section_obj.getboolean("is_academic"),
                "setrealname": section_obj.getboolean("setrealname"),
            }
        except ValueError:
            print(f"ERROR: Invalid section '{section}'!")
            return None

        if len(section_obj.keys()) != len(cur):
            print(f"ERROR: Got invalid amount of keys in {section}")
            return None

        print(f"{section} config is valid!")

    return ret


server_configs = read_and_validate_config("server_config.ini")
if __name__ == "__main__":
    if not server_configs:
        sys.exit(1)
