import os
import subprocess
import tempfile
import contextlib
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
def run_binary(binary, *args, gdb=None, **kwargs):
    for name in ["stdin", "stdout", "stderr"]:
        kwargs.setdefault(name, subprocess.PIPE)

    cmd = []
    if gdb:
        with tempfile.NamedTemporaryFile("w", delete=False) as script:
            script.write(gdb)
        cmd += ["/usr/bin/gdb", "-x", script.name, "--batch", "--args"]

    with temp_binary(binary) as binary_path:
        cmd += [binary_path, *args]
        process = subprocess.Popen(cmd, **kwargs)
        try:
            yield process
        finally:
            process.kill()
            if gdb:
                os.unlink(script.name)


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
        self.codes = []

    @property
    def code(self):
        labels = {}
        for code in self.codes:
            label = None
            content = ""
            for line in code.split("\n"):
                line = line.strip()
                if not line.startswith(".") and ":" in line:
                    if label:
                        labels[label] = content
                    label = line[: line.index(":")]
                    content = ""
                content += f"{line}\n"
            if label:
                labels[label] = content

        code = textwrap.dedent(
            """
            .intel_syntax noprefix
            .global _start
            .set SYS_write, 1
            .set SYS_exit, 60
            .section .text

            """
        )

        for content in labels.values():
            if not content.endswith("\n"):
                content += "\n"
            code += content

        return code

    def do_execute(
        self, code, silent, store_history=True, user_expressions=None, allow_stdin=False
    ):
        if not silent:
            try:
                results = dict()
                if not code.startswith("%"):
                    self.codes.append(code + "\n")
                    if "_start" in code:
                        results = self.do_run()
                else:
                    magics = {
                        "readelf": self.magic_readelf,
                        "asm": self.magic_asm,
                        "gdb": lambda: self.magic_gdb(code),
                    }
                    magic = code.split()[0][1:]
                    if magic in magics:
                        results = magics[magic]()

                html = ""
                for key, value in results.items():
                    if isinstance(value, bytes):
                        value = value.decode("latin")
                    html += f"<h3>{key}</h3>"
                    html += f"<code>{value}</code>"

                display_data = {
                    "metadata": {},
                    "data": {"text/html": html},
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

    def do_run(self):
        binary = build_binary(self.code)
        with run_binary(binary) as process:
            stdout, stderr = process.communicate()
            returncode = process.returncode
        return dict(stdout=stdout, stderr=stderr, returncode=returncode)

    def magic_readelf(self):
        binary = build_binary(self.code)
        with temp_binary(binary) as binary_path:
            readelf = subprocess.check_output(["/usr/bin/readelf", "-a", binary_path])
            return dict(readelf=readelf)

    def magic_asm(self):
        return dict(asm=self.code)

    def magic_gdb(self, code):
        script = code.split("\n", 1)[1]
        binary = build_binary(self.code)
        with run_binary(binary, gdb=script) as process:
            stdout, _ = process.communicate()
        return dict(gdb=stdout)
