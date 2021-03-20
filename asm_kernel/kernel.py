import os
import subprocess
import tempfile
import contextlib
import math
import textwrap

from ipykernel.kernelbase import Kernel


def build_binary(source):
    cmd = [
        "/usr/bin/gcc",
        "-x",
        "assembler",
        "-nostdlib",
        "-no-pie",
        "-",
        "-o",
        "/dev/stdout",
    ]
    binary = subprocess.check_output(cmd, input=source.encode(), stderr=subprocess.PIPE)
    return binary


@contextlib.contextmanager
def temp_binary(binary):
    with tempfile.NamedTemporaryFile(delete=False) as f:
        f.write(binary)
    path = f.name
    os.chmod(path, 0o755)
    try:
        yield path
    finally:
        os.unlink(path)


@contextlib.contextmanager
def run_binary(binary, *args, gdb_script=None, **kwargs):
    for name in ["stdin", "stdout", "stderr"]:
        kwargs.setdefault(name, subprocess.PIPE)

    cmd = []
    if gdb_script:
        with tempfile.NamedTemporaryFile("w", delete=False) as script:
            script.write(gdb_script)
        cmd += [
            "/usr/bin/gdb",
            "-x",
            script.name,
            "--batch",
            "-return-child-result",
            "--args",
        ]

    with temp_binary(binary) as binary_path:
        cmd += [binary_path, *args]
        process = subprocess.Popen(cmd, **kwargs)
        try:
            yield process
        finally:
            process.kill()
            if gdb_script:
                os.unlink(script.name)


def html(element):
    def html_recursive(element):
        if isinstance(element, dict):
            assert len(element) >= 1
            node, *attrs = list(element.items())
            name, value = node
            attrs = " ".join(f'{attr}="{value}"' for attr, value in attrs)
            yield f"<{name} {attrs}>" + "".join(html_recursive(value)) + f"</{name}>"
        elif isinstance(element, (list, tuple)):
            for value in element:
                yield from html_recursive(value)
        else:
            yield str(element)

    return "".join(html_recursive(element))


class ASMRepl:
    asm_prefix = textwrap.dedent(
        """
        .intel_syntax noprefix
        .global _start
        .set SYS_write, 1
        .set SYS_exit, 60

        .section .text

        """
    )
    asm_suffix = ".end: nop\n"

    def __init__(self):
        self.code_blocks = []
        self.gdb = None
        if self.__class__ == ASMRepl:
            raise NotImplementedError()

    @property
    def code(self):
        code = self.asm_prefix
        code += "".join(f"{block}\n" for block in self.code_blocks)
        code += self.asm_suffix
        code = code.strip()
        return code

    @property
    def gdb_script(self):
        return None

    def read(self, code):
        raise NotImplementedError()

    def evaluate(self, gdb_script=None):
        gdb_script = gdb_script or self.gdb_script
        binary = build_binary(self.code)
        print(repr(gdb_script), flush=True)
        with run_binary(binary, gdb_script=gdb_script) as process:
            stdout, stderr = process.communicate()
            returncode = process.returncode
        return dict(stdout=stdout, stderr=stderr, returncode=returncode)

    def reset(self):
        self.code_blocks.clear()


class EditableASMRepl(ASMRepl):
    def __init__(self):
        super().__init__()
        self.labels = {}
        self.code_blocks = self.labels.values()

    def read(self, code):
        label = None
        content = ""
        for line in code.split("\n"):
            line = line.strip()
            if not line.startswith(".") and ":" in line:
                if label:
                    self.labels[label] = content
                label = line[: line.index(":")]
                content = ""
            content += f"{line}\n"
        if label:
            self.labels[label] = content
        return "_start" in code

    def reset(self):
        self.labels.clear()


class LinearASMRepl(ASMRepl):
    asm_prefix = ASMRepl.asm_prefix + "_start:\n"

    @property
    def gdb_script(self):
        def tbreak(address):
            return [
                f"tbreak {address}",
                "commands",
                f'printf "###start registers {address}\\n"',
                "info registers",
                f'printf "###end\\n"',
                "continue",
                "end",
            ]

        script = []
        for block_id in range(len(self.code_blocks)):
            script += tbreak(f".block.{block_id}")
        script += tbreak(".end")
        script += ["run"]
        return "\n".join(script)

    def read(self, code):
        block_id = len(self.code_blocks)
        code = f".block.{block_id}:\n" + code
        self.code_blocks.append(code)
        return True

    def evaluate(self, *args, **kwargs):
        result = super().evaluate(*args, **kwargs)

        gdb_output = result["stdout"].decode()
        state = None
        block = None
        gdb_data = {}
        for line in gdb_output.split("\n"):
            if line.startswith("###start"):
                _, state, block = line.split()
                gdb_data[block] = {}
            elif line == "###end":
                state = None
            elif state == "registers":
                register, value, extra = line.split(maxsplit=2)
                gdb_data[block][register] = (value, extra)

        return self.print_result(gdb_data)

    def print_result(self, gdb_data):
        def register_values(block_id):
            return {
                k: (v[0] if k != "eflags" else v[1])
                for k, v in gdb_data[block_id].items()
                if k not in ["cs", "ss", "ds", "es", "fs", "gs"]
            }

        registers = register_values(".end")
        last_block_id = len(self.code_blocks) - 1
        changed = {
            k
            for k, v in register_values(f".block.{last_block_id}").items()
            if v != registers[k]
        }

        def register_style(register):
            if register in changed:
                return "background-color: rgba(255, 0, 255, 0.05)"
            return ""

        registers = [
            {
                "div": [{"b": k}, {"div": v}],
                "style": f"text-align: center; {register_style(k)}",
            }
            for k, v in registers.items()
        ]

        columns = 3
        rows = math.ceil(len(registers) / columns)

        def register_element(row, column):
            return registers[len(registers) // columns * column + row]

        result = {}
        result["registers"] = html(
            {
                "table": {
                    "tbody": [
                        {
                            "tr": [
                                {"td": register_element(row, column)}
                                for column in range(columns)
                            ]
                        }
                        for row in range(rows)
                    ]
                },
                "class": "table table-bordered",
                "style": "font-family: monospace",
            }
        )
        return result


class ASMKernel(Kernel):
    implementation = "ASM"
    implementation_version = "0.1"
    language_info = {
        "name": "gas",
        "architecture": "x86",
        "mimetype": "text/x-gas",
        "file_extension": ".s",
    }
    banner = "ASM kernel"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # self.asm_repl = EditableASMRepl()
        self.asm_repl = LinearASMRepl()

    def do_execute(
        self, code, silent, store_history=True, user_expressions=None, allow_stdin=False
    ):
        if not silent:
            try:
                results = dict()
                if not code.startswith("%"):
                    if self.asm_repl.read(code):
                        results = self.asm_repl.evaluate()
                else:
                    magics = {
                        "readelf": self.magic_readelf,
                        "asm": self.magic_asm,
                        "reset": self.magic_reset,
                        "gdb": lambda: self.magic_gdb(code),
                    }
                    magic = code.split()[0][1:]
                    if magic in magics:
                        results = magics[magic]()

                output = ""
                for key, value in results.items():
                    output += html({"h3": key})
                    if isinstance(value, str):
                        output += value
                    else:
                        if isinstance(value, bytes):
                            value = value.decode("latin")
                        output += html({"code": value})

                display_data = {
                    "metadata": {},
                    "data": {"text/html": output},
                }
                self.send_response(self.iopub_socket, "display_data", display_data)

            except subprocess.CalledProcessError as e:
                stderr = e.stderr.decode("latin")
                stream = {"name": "stderr", "text": stderr}
                self.send_response(self.iopub_socket, "stream", stream)

        return {
            "status": "ok",
            "execution_count": self.execution_count,
            "payload": [],
            "user_expressions": {},
        }

    def magic_readelf(self):
        binary = build_binary(self.asm_repl.code)
        with temp_binary(binary) as binary_path:
            readelf = subprocess.check_output(["/usr/bin/readelf", "-a", binary_path])
            return dict(readelf=readelf)

    def magic_asm(self):
        return dict(asm=self.asm_repl.code.encode())

    def magic_reset(self):
        self.asm_repl.reset()

    def magic_gdb(self, code):
        _, _, script = code.partition("\n")
        binary = build_binary(self.asm_repl.code)
        with run_binary(binary, gdb_script=script) as process:
            stdout, _ = process.communicate()
        return dict(gdb=stdout)
