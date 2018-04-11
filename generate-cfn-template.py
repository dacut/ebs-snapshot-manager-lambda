#!/usr/bin/python3
import re

include_re = re.compile(r"(?P<indent> *)@include\((?P<filename>[^)]+)\)")

with open("cloudformation.yml.in", "r") as ifd:
    with open("cloudformation.yml", "w") as ofd:
        for line in ifd:
            m = include_re.match(line)
            if not m:
                ofd.write(line)
            else:
                indent = m.group("indent")
                filename = m.group("filename")

                with open(filename, "r") as include_fd:
                    for line in include_fd:
                        ofd.write(indent + line)
