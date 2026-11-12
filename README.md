# Classetta (previously MacRT)

A runtime for classic Mac OS (System 1–7) applications with a PyQt6 front end.

Classetta runs a 68k application's code on an emulated CPU. Rather than using an actual Macintosh ROM to provide the Macintosh Toolbox A-traps to the application, Classetta provides a compatible implementation of the Macintosh Toolbox in Python. The user interface is predominantly implemented with native Qt components, rather than just drawn in pixels on an emulated screen.

Instead of a single window for your entire virtual Macintosh, each application's window can exist as a separate native window on your host OS.

Alternatively, they can also exist inside a single window for the virtual Mac's screen.

![Three 68k test applications running side by side](docs/screenshots/test-apps.png)

## What runs

While development is still very much in progress, a large number of applications are already working. TeachText, Microsoft Excel, MacWrite, MacPaint and Microsoft Word 1.0 all run. Some games such as Creepy Castle and Glider+ have also been tested.

[A full list of tested apps is here.](docs/titles.md) Want to run something else? Try it and see how far it gets!

## Details

The [Unicorn](https://github.com/unicorn-engine/unicorn) framework is used for processor emulation.
Depending on the configuration, it emulates either a 68000, 68020 or 68030 CPU (the latter two with a 68881 FPU).

The toolbox implementation features over 700 routines (and more to come!):

  - QuickDraw: regions, pictures, icons, colour ports, pixel patterns, offscreen GWorlds, palette manager and the color picker.
  - Apple Events: the Apple Event Manager (inter-application events not yet supported).
  - Edition Manager: pub/sub features.
  - SANE (Standard Apple Numerics Environment): floating point arithmetic, conversions and elementary functions.
  - Printing Manager: print jobs generate a PDF on the host.
  - Sound Driver: three synthesizers, Sound Manager channels, sampled sounds playbacks as well as legacy direct access to the sound buffer on earlier target systems.
  - Other managers implemented include: Memory, Resource, Window, Menu, Event, Control, Dialog, List, Scrap, File, Device, Process, TextEdit and Standard File.

A MultiFinder-esque implementation is also present, allowing multiple applications to run at once.

File I/O is implemented for MacBinary, BinHex, AppleSingle, AppleDouble, MFS/HFS/HFS+ disk image, MOOF floppy image and CD-ROM image. An application started from a file on the host system's disk can read and write files on the host's filesystem.

Still to come: Apple Events, PPC (Program-To-Program) Toolbox Sessions, serial ports, AppleTalk, support for applications which use custom Open/Save dialogues and text-to-speech. The implementation is intended to gracefully degrade in the event unimplemented features are hit, in the hope of avoiding a crash.

## Quick start

```sh
python -m pip install -e .              # Installs dependencies including: PyQt6, unicorn, rsrcfork, machfs
python -m macrt tests/apps/Hello.bin    # Run a small test application
```

If you see a window and a menu bar, everything's installed correctly.

```sh
python -m macrt App.bin                            # MacBinary, BinHex, AppleDouble…
python -m macrt "System 6.0.8.dsk" --app TeachText # Run from a disk image. Path for --app is colon separated.
python -m macrt One.bin Two.hqx                    # Run more than one app simultaneously
python -m macrt App.bin --view                     # Just browse an app's resources, rather than running the app.
```

Further reading is provided at: [Running a Mac application](docs/running-apps.md).

## Documentation

[An overview of the documentation](docs/README.md) is provided here:

- **[Running a Mac application](docs/running-apps.md):** what to install,
  getting an application to macrt with its resource fork, every option,
  and what to do when it stops.
- **[Titles tried so far](docs/titles.md):** the applications run so far,
  how far each gets, and what to give it on the command line.
- **[What works](docs/toolbox.md):** the Toolbox and Operating System,
  a manager at a time, with where macrt differs from a Mac and why.
- **[How macrt works](docs/architecture.md):** the processor, the trap
  dispatcher, memory, drawing, resources and files, and the source tree,
  for readers who know the 68k and the Mac's insides.
- **[The debugger, and MCP](docs/debugger.md):** the debugger window,
  the MCP server, and the `ram:` addresses that tie it to Ghidra.
- **[Working on macrt](docs/development.md):** the tests, the 68k test
  applications, and adding a Toolbox routine.
- **[Surveying many applications](docs/survey.md):** running hundreds of
  titles headless and comparing the results before and after a change.

## Next

1. Suspend and resume when a host window gains or loses focus, so the
   clipboard follows without an extra click.
2. What [Not yet](docs/toolbox.md#not-yet) lists.
3. More real applications, fixing whatever each one reaches first (see
   [the ones tried so far](docs/titles.md)).


| Emulated screen, global menu bar | Host windows, menu bar under each title bar |
|---|---|
| ![](docs/screenshots/screen-global.png) | ![](docs/screenshots/desktop-window.png) |
