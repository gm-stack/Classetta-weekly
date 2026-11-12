# The debugger, and MCP

Part of [the documentation](README.md).

`--debug` opens the debugger window, and `--mcp` answers MCP over HTTP on
localhost so a client like Claude Code gets the same thing:

```sh
python -m macrt App.bin --mcp                 # http://127.0.0.1:8765/mcp
python -m macrt App.bin --mcp 9000 --symbols syms.json
pip install -e '.[mcp]'                       # what --mcp needs
```

and in the client's config (the emulator prints this stanza at startup, with
the port it actually got):

```json
{"classetta": {"type": "http", "url": "http://127.0.0.1:8765/mcp"}}
```

Thirty-eight tools: run control (`status`, `pause`, `step`, `cont`),
breakpoints by address, trap or memory access, memory (`read_memory`,
`write_memory`, `dump_ram`), symbols (`find_symbol`, `call_function`), and
everything a `--script` can do — `screenshot`, `press`, `type_text`, `click`,
`wait_window`.  `command` takes a line of the window's own grammar, so `b
$1000` and the `breakpoint` tool arm one and the same breakpoint.

State is read when asked about, so there's nothing to stream: the server is
stateless and answers each request with one JSON body, which is also what lets
two clients look at one game.  While the program *runs* there is no consistent
machine to read — the guest thread is inside the CPU — so `status` says only
that; `pause` first, and it answers with the registers, the code around the PC
and why it stopped.

A **`ram:`** address is Ghidra's number for the same byte.  The image Ghidra
analysed was assembled statically and the code runs relocated, in segments,
under a different A5, so any address anywhere in the debugger or the tools can
be written `ram:0012ABCD` and it's translated through the running
application's segment map.  A `ram:` address whose segment isn't loaded is an
error rather than a breakpoint that never fires.

Nothing in either the window or the server touches the core or a widget
directly: the guest thread owns the 68k CPU and the GUI thread owns the
screen, so the server is a third thread that only ever asks — `macrt.dbg` for
the debugger, the display's bridge for anything on screen.  `macrt/dbg.py` is
the one Qt-free source of truth for the commands, shared by both, so the two
are looking at one dump page and one set of breakpoints.

`tests/test_debugger.py`, `tests/test_debug_commands.py` and
`tests/test_mcp_server.py` cover the core's stop-and-snapshot machinery, the
command grammar including `dm`/`sm` and `ram:`, and the server — driven by the
MCP SDK's own client over a real socket, plus raw HTTP for the malformed body,
the wrong `Content-Type` and the web page that resolved `localhost` to this
port.  `tests/test_trap_trace.py` runs `--trace-traps`' argument readers over
every trap in the tables, since nothing else in the suite traces and a trap
line is the first thing a broken one of them takes down: the game died at its
first `ReserveMem`.
