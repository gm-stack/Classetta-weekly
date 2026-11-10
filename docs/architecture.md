# How macrt works

Part of [the documentation](README.md).

> [!NOTE]
> This page assumes some knowledge of 68k Classic Mac hardware and software.

This page details how the 68k code is run, how traps are handled in Python, the virtual Mac's memory map and how the PyQT6 based windows interact with the classic Mac code.

It will then go on to describe the launch process of applications.

- [The big picture](#the-big-picture)
- [The processor](#the-processor)
- [Traps](#traps)
- [Memory](#memory)
- [Drawing and the display](#drawing-and-the-display)
- [Resources and files](#resources-and-files)
- [Text and fonts](#text-and-fonts)
- [The source tree](#the-source-tree)


## The big picture

Classetta runs the application's 68k code on an emulated CPU. This catches A-line traps which are dispatched to Python code.

```
guest thread (one per app)                         GUI thread
──────────────────────────                         ─────────────────────────────────
68k code on the CPU (m68k/cpu.py)
  └─ A-line trap ─▶ dispatcher (m68k/dispatch.py)
       └─ glue (m68k/glue_*.py)
            └─ Toolbox (toolbox/api.py) ─────────▶ GuiBridge.call ──▶ Display (ui/display.py)
                 WaitNextEvent ◀── EventQueue ◀──────────────────────── InputRouter (Qt events)
                 FindWindow, MenuSelect,                                     │
                 TrackGoAway, DragWindow… ◀── Future ◀── Trackers ◀──────────┘
```

The `Machine` in `m68k/runtime.py` implements the CPU and the trap stubs.

Every running application sits inside a `Runner` on its own thread.

As under MultiFinder, only one application runs on the CPU at a time. When it executes `WaitNextEvent`, `GetNextEvent` and other similar calls, it gives up the CPU and another application can then execute. Registers and low memory globals are restored when this switch occurs.

Classetta reads the 156-byte `WindowRecord` structures in the app's heap to retrieve titles, port rectangles, regions, hilite states and zoom states. A `MacWindowView` then draws based on the contents of these records in the Mac's memory. This means that applications that alter these directly are still compatible.

## The processor

The processor is implemented in Unicorn.

It currently runs in one of the following modes:

`--model plus`: (default) Emulate a Mac Plus - 68000 CPU and no FPU. Compatibility with earlier apps.
`--model ii`: Emulate a Mac II - 68020 CPU, 68881 FPU.
`--model iix`: Emulate a Mac IIx - 68030 CPU, 68882 FPU.

The Mac II emulates Apple's HMMU rather than a full PMMU - under Mac OS, the PMMU is only required for virtual memory (which is out of scope for Classetta).

Currently all are emulated in 24 bit mode.

The difference between these CPUs come in availability of certain instructions and the exception frames that are returned. Some applications require an earlier CPU as they attempt to manipulate exception frames directly.

A few instructions in Unicorn's 68020/68881 do not seem to be implemented for all the ways they are used in Mac software (`CHK2`, `CMP2`, `PACK`, `UNPK`, `FMOVE` on packed decimals, `FMOVE` of immediate data to the floating-point control registers, `FSAVE` and `FRESTORE`). These have been re-implemented in Python (see `macrt/m68k/instructions.py`) and their implementation replaces Unicorn's.

Support for self-modifying code is implemented as well. Unicorn features JIT (Just-In-Time translation) caching for greater performance. The processor translates blocks of 68k code into native code, and re-runs these cached blocks instead of re-translating the original each time. If it sees a write into a block it has translated, this cache will be invalidated. If it is currently running that block, execution will be halted and restarted at an appropriate point (Microsoft Word is a particular offender here - see `m68k/cpu.py`).

By default 68k code is run without any artificial slowdown. The option `--speed N` will enable a mode which tallies up the number of cycles that each instruction would have taken on a real 68000 and inserts small periodic delays to limit execution speed to approximately N times the speed of a real Mac Plus's CPU (see `m68k/cycles.py`).

## Traps

The designers of the Motorola 68000 CPU deliberately left two large chunks of opcodes unimplemented for expansion purposes - all instructions beginning with `$A` and `$F`. The instructions beginning with `$F` were later used to perform floating point operations with an attached 68881 or 68882 FPU. The instructions beginning with `$A` were left open for system vendors to use as they like.

Upon executing one of these unimplemented instructions, the CPU jumps to Vector #10 (for `$A`) or Vector 11 (for `$F`). This differs from an illegal instruction, one which Motorola has specified is invalid - these are routed to Vector #4 and will trigger a system error.

The classic Mac OS makes extensive use of these instructions - using instructions from `$A` for applications to make system calls. These are known as "A-Line Traps" or simply "Traps".

`cpu.run()` will run the next block of code on the emulated CPU. When it hits an A-line trap (among other things), this function will return.

The dispatcher (see `m68k/dispatch.py`) looks for the particular trap's instruction ID in `m68k/trapdefs.py`. This is code generated from the Multiversal interface files, providing information on the function's arguments (in stack, or in registers), how the result should be returned, and a 'selector' in case the same opcode will trigger one of a number of traps depending on arguments (e.g. `_Pack3` or `_OSDispatch`). Then an appropriate Python routine (designated by the decorator `@trap`) is executed, the result put back to the emulated CPU/memory in an appropriate manner, and execution resumed.

All trap instructions begin with `A`. The lower 9 bits of the instruction are used to determine which trap is being called, with the 11th bit being 1 for Toolbox traps, and 0 for OS level traps (for more info, see [About the Trap Manager](https://preterhuman.net/macstuff/techpubs/mac/OSUtilities/OSUtilities-170.html) from Inside Macintosh).

There are some behaviours that have to be worked around, however. Earlier Mac ROMs only decode the lower 8 bits, so the 9th bit is ignored. For an example of where this resulted in a problem - Crozzle 0.8  calls `NewWindow` as `$AB13` rather than the correct `$A913`, resulting in a crash. Classetta takes a dynamic approach to this - if bit 9 is set, and the trap doesn't match anything known, the corresponding trap with bit 9 cleared will be used. This will not produce 100% compatibility but allows this issue to be resolved most times without having to implement separate behaviours.

OS-level traps (`$A000`-`$A7FF`) have arguments provided in the CPU`s registers, primarily A0/D0. Often these include bitfield flags mixed with parameters in a single register. These will also update CPU flags based on D0 so that the branch instructions can then branch on the result.

Toolbox traps (`$A800`-`$AFFF`) have arguments provided in the same manner as Pascal function calls, provided on the stack. There is allocated space for the result first, followed by arguments in order (last argument is at the uppermost address). The handler will pop all the arguments from the stack and write the result in.

Bit 10 of the opcode is known as the 'auto-pop' bit, if set will pop 4 bytes from the stack before dispatching to the toolbox routine, so that if function A JSRs to a subroutine B which calls the toolbox trap, control will actually return directly to function A rather than B.

If a trap is documented but Classetta does not yet implement it, it will be logged and skipped. Arguments will be popped from the stack (nothing will be done with them), and the trap will return `unimpErr` for dispatch traps or `0` otherwise. If it is undocumented, a crash with System Error 12 will be generated - continuing to execute code will not be possible because we don't know how many bytes to remove from the stack to pop (and ignore) the arguments.

## Patching

`SetTrapAddress` can be used to change the handler for a particular trap. `GetTrapAddress` can be used to obtain the address of the trap handler. Frequently this is used so that the new routine can call the old one as part of its work. So our trap handler must be able to handle a direct call to the function via its memory address, not just an A-line instruction.

This handler is called in the same way the Mac's own dispatcher does, saving registers on the stack in the same format (since some patches use this data in the handler).

`GetTrapAddress` will return unimplemented for `_OSDispatch` even though we do have it implemented. Some programs will use this trap as a method of determining if MultiFinder is running - if they see this trap is implemented, they'll then try to make private calls to MultiFinder itself that will subsequently fail, as we do not have MultiFinder actually running (Microsoft's apps frequently do this).

Filter procedures, `CDEF`s, user items, action procedures and patched traps are implemented in 68k code that will be called by the toolbox. In order to handle these, `Runner.call()` will run the function, returning when the function returns.

More information on trap routines can be found in [Working on macrt](development.md#adding-a-toolbox-routine).


## Memory
<!--
  TODO:
  - is this address map correct?
  - is that the correct criteria for 512x342 screen placement?
-->

`System` (in `toolbox/system.py`) implements the following address space:

```
$FFFFFF  top of the screen buffer (extending downwards from here)
$100000  one partition per app, sized by its SIZE resource (at least 1MB)
$003000  system heap: Window Manager port, GrayRgn, System file resource map, defproc stand-ins
$000000  low-memory globals: MemTop, ScrnBase, WindowList, MenuList, CurrentA5 and more
```

If the screen is 512x342, it will be placed at `$7A700` with an alternate buffer at `$72700`. Many early games will hard-code this address rather than query `ScrnBase` (see [The
hardware](toolbox.md#the-hardware)). The partitions take the rest of RAM above this.

Each of the memory partitions is laid out like a classic Mac's application: heap at the bottom, A5 world at the top, with stack below it (extending downwards). The QuickDraw globals (thePort, patterns, arrow cursor, screenBits) exist at the bottom of A5.

The Memory Manager (`memmgr.py`) deals with pointers and handles using the 24-bit block header (tag byte, physical size, relative handle or zone pointer) and master pointers with their flag byte (locked, purgeable, resource). Code that manipulates these directly will see them as it expects (compatibilty with "32 bit dirty" apps is retained). The code will not move any handles marked as "locked" to preserve this guarantee.

When an allocation is made for greater than the amount of available memory, the application heap grows towards `ApplLimit`. Programs written before `MaxApplZone` was added may ask for an excessively large number in order to signify they want to allocate all avaliable memory. Compatibility with this should be retained. Some applications like MacWrite count free memory by looking at block headers, so we need to retain compatible structures with the classic Mac OS.

A purged handle keeps the master pointer as NIL, just like `EmptyHandle` would do on real Mac OS.

See [What works](toolbox.md#memory-manager) for details on compaction, purging, temporary memory and grow-zone.

Under MultiFinder some of the low-memory globals belong to the running app (`WindowList`, `MenuList`, `CurrentA5`, `ApplZone` etc). Each app has their own copy in an "A5 World" (see [The A5 World](https://preterhuman.net/macstuff/techpubs/mac/runtimehtml/RTArch-116.html)), implemented in `lowmem.py` as `World` which is swapped before execution of that app begins on the CPU.

In `windows.py`, `menus.py` and`quickdraw.py`, the `WindowRecord`, `WindowList`, `MenuInfo` and `MenuList` structures are implemented as views over the bytes in memory, with the in-memory layout being identical to the documented layout in Inside Macintosh. These classes are backed by the actual Mac's memory, so the UI is drawn with reference to what the Mac actually has in memory.

### The stack

An application is allocated 32K of stack from `ApplLimit` to `curStackBase` (the variable `DefltStack` reflects this). Note that this is a little different from a real Mac, where it's 8kB without colour QuickDraw, and 24K with it.

`ReserveMem` will grow the size of the heap toward `ApplLimit` as long as there's still space. If that's more than is available, it will simply allocate what's avaliable and return the size of the allocation.

`SetApplLimit` will never shrink the stack below `DefltStack`. This is important to make certain apps like Macify 2.7 and Bumptz Science Carnival work, as they try to do this (but would immediately crash if it was actually done).

## Drawing and the display

QuickDraw, implemented in `qdraw.py`, draws into an image for each window (`Backing`) or a `portBits` region in the Mac's memory (offscreen drawing and `CopyBits` will work). Regions are implemented as actual QuickDraw regions, manipulated in guest memory.

Text width is correctly implemented, even with different fonts picked by the host.

The screen buffer in `screen.py` will mirror the windows. QuickDraw draws into the window's buffer, and then the window in drawn next time a trap is hit, or if no traps occur, once per tick (1/60th of a second).

Window functionality is not implemeted natively inside Qt. Every click is sent to the emulated Mac as a `mouseDown` event in the app's queue. For example: when closing a window, if the app was to call `TrackGoAway` (the function called when a close box is clicked), the close box then re-draws as pressed - because the emulated Mac called the function, not because there is a handler in Qt to draw this.

Functions like `MenuSelect`, `TrackGoAway`, `TrackBox`, `DragWindow` and `GrowWindow` will start a tracker on the GUI thread, awaiting completion of a `Future` from mouse events.

`ui/chrome.py` implements WDEF related logic. It has functions for geometry, `wHit` codes, painting, covering document windows, `dBoxProc`, `plainDBox`, `altDBoxProc`, `movableDBoxProc` and
  `rDocProc``dBoxProc`, `plainDBox`, `altDBoxProc`, `movableDBoxProc` and `rDocProc`. Everything is drawn with `fillRect` quantized to whole pixels.

If an application provides custom `WDEF` or `MDEF` resources, these are run as native 68k code instead (see `m68k/wdefs.py`, `m68k/mdefs.py`).

![](screenshots/window-kinds.png)


## Resources and files

`toolbox/resources.py` is the Resource Manager. As on a Mac, resource maps
live in the heap in their on-disk format, so 68k code that walks one finds
what it expects, and resources are read in through `_ResourceStub`, so an
application's patch on it, given a map's handle in A4 and an entry in A2,
can read them in its own way.

* **Files.** `macfile.load_file` reads a Mac file in whatever form the host
  keeps it: MacBinary I/II/III, BinHex 4.0, AppleSingle, AppleDouble
  (`._app`, `.AppleDouble/app`, or `app.rsrc` as The Unarchiver writes it),
  Basilisk II's `.rsrc/` and `.finf/`, Executor's `%app`, a bare `app.rsrc`,
  or macOS's `..namedfork/rsrc`. `open_application` is what the command line
  uses: it also opens disk images and refuses StuffIt and zip archives with a
  hint.
* **Disk images.** `load_volume` opens HFS disk images, raw, DiskCopy
  4.2, behind an Apple partition map or of a CD's whole 2352-byte
  sectors, into machfs's form, walking their catalog and extents trees
  with machfs's B*-tree reader but leaving each file's forks on the disk
  until they're read (a CD's image, or any of 16 MB or more, is mapped
  rather than read in, but for the data of a raw CD image's sectors). It
  reads MFS (400K) disks into the same form: their one flat directory,
  each fork followed through the block map. HFS Plus volumes, on their
  own, in an HFS wrapper or in an `Apple_HFS` or `Apple_HFSX` partition,
  are read into it too (`_read_hfs_plus`, from Technote 1150), with their
  dates made local, hard links made the files they link to, and names
  HFS can't hold cut short and given their catalog node ID. A MOOF image
  of a floppy's tracks is decoded to its sectors first (`macrt.moof`):
  the GCR bytes found in each track's bits, and each sector's three bytes
  to four of them, unmixed from the checksum that runs through them. The
  volume keeps its disk's sectors and their tags as `.disk`, for the
  floppy disk driver (`glue_sony`), and a MOOF image's tracks with them.
  A `Folder` finds files by Mac name, case-insensitively, and
  `OpenResFile` looks names up in the folder the app came from.
* **The chain.** Opening a file copies its map into a handle, fills in
  `nextMap` and the file reference number, and puts it at the top of the
  app's chain (`TopMapHndl`, `CurMap`). Every chain ends at the System file's
  map in the system heap (`SysMapHndl`, `SysMap` = 2). If no System file is
  given, an empty one is made. Either way the WDEF, MDEF, CDEF, LDEF and MBDF
  stand-ins are resources in it, so `GetResource('WDEF', 0)` works and a
  window's `windowDefProc` is a real resource handle. So are the standard
  resources in `sysres.py` that the file lacks: `PACK` 0 to 15 (each the
  package's trap with auto-pop, so calling into the package is the same
  as calling the trap), the alert icons `ICON` 0 to 2 and the cursors
  `CURS` 1 to 4.
* **Loading.** The entry in the map holds the resource's handle, so asking
  twice gives the same handle. Attributes set the master pointer flags:
  locked, purgeable, `resSysHeap` (loaded into the system heap) and
  `resPreload` (loaded at open). A purged resource is read again into the same
  handle. System 7 compressed resources are expanded through rsrcfork; one
  that won't expand gives `CantDecompress`, unless the `dcmp` its header
  names is there to run (`m68k/decompressors.py`).
  `SetResLoad(false)`, `Get1…` and `UseResFile` behave as documented.
* **Changes.** `AddResource`, `ChangedResource`, `RemoveResource`,
  `SetResInfo` and `SetResAttrs` work on the map in memory. `UpdateResFile`
  and `CloseResFile` write the fork back through the File Manager, which
  keeps it where [Documents and
  saving](running-apps.md#documents-and-saving) says a program's files go.
* **The application.** A launch opens the app's resource fork as its current
  file (`CurApRefNum`) and sizes its partition from its SIZE resource.
  Quitting closes every file the app opened.

`toolbox/templates.py` parses WIND, DLOG, ALRT (with its four stages), DITL,
CNTL, MBAR and STR#, and applies System 7 positioning words such as
`centerMainScreen` and `alertPositionMainScreen`. On top of these:
`GetNewWindow`, `GetMenu` (the MENU resource *is* the MenuInfo; its MDEF ID
becomes the MDEF's handle), `GetNewMBar`, `SetMenuBar`, `GetMenuBar`,
`GetString` and `GetIndString`. The Dialog and Control Managers build their
dialogs, alerts and controls from DLOG, ALRT, DITL and CNTL.

The same Toolbox serves guest applications written in Python, as the
demo's are:

```python
from macrt.macfile import load_file

app = load_file("Hello.bin")            # or ._Hello, Hello.rsrc, …
display.launch(app.name, guest, app=app)
# guest(tb): tb.SetMenuBar(tb.GetNewMBar(128)); w = tb.GetNewWindow(128) …
```


## Text and fonts

Your computer's fonts stand in for the standard families (Chicago,
Geneva, New York, Monaco and the rest); [Running a Mac
application](running-apps.md#fonts) says how to give macrt a Chicago
and a Geneva. A family an application brings with it, as a `FONT`, or
as an `NFNT` its `FOND` lists, is drawn from the font's own bitmap
(`toolbox/bitfont.py`). That matters because such fonts often hold
pictures rather than letters: MacPaint's tool palette is one, and games
keep sprites and score digits in them. A size the application doesn't
have is scaled from the nearest one it does. A standard family is drawn
from a strike too when the application carries its own of the size, as
on a Mac, where the application's file is looked in before the System's:
Balance of Power traces its New York 36 title letter by letter and needs
them where they were. Text in such a font is measured with the font's
own widths too, by `StringWidth`, `MeasureText`, TextEdit and
`TETextBox` alike, so lines break where the application expects. Bold,
italic, underline, outline and shadow are drawn as QuickDraw draws them.
What a Mac font has no width for, such as NUL and return in Chicago,
Geneva and Monaco, draws nothing, where a host font would draw a box.

A real System file, or the disk's own, also gives the standard families
its strikes' widths and line spacing (see [A real System
file](running-apps.md#a-real-system-file)).


## The source tree

```
macrt/
  memory.py    guest RAM (big-endian, 24-bit addresses) and typed record views
  resfork.py   the resource fork format, read (every offset checked) and written
  macfile.py   Mac files from the host: MacBinary, BinHex, AppleSingle/Double,
               emulator side files, HFS, HFS Plus and MFS disk images
  moof.py      Applesauce MOOF floppy images: each track's GCR bits decoded
               back to sectors
  process.py   one running guest application
  dbg.py       the debugger's commands and the rules about what's safe to read
               while the machine runs — Qt-free, so the window and the MCP
               server share one set of commands
  mcpserver.py the MCP server on localhost: the tools, on a thread that only
               ever asks the guest thread and the GUI thread
  inject.py    hooks on the application's own routines, for tracing and for
               calling into it
  viewer.py    the resource listing, and ResourceViewer, which shows an
               app's menus and windows from its resources
  demo.py      the command line, and the demo's Sampler and Ticker
  survey.py    the survey tool (docs/survey.md)

  m68k/        running 68k code
    cpu.py           the processor: unicorn's core on the guest RAM
    instructions.py  what unicorn doesn't run, run in Python
    cycles.py        a 68000's instruction timings, for --speed
    runtime.py       Machine, Runner, system errors
    dispatch.py      the A-line trap dispatcher
    trapdefs.py      trap signatures, generated by tools/gen_trapdefs.py
    segload.py       CODE 0, the jump table, LoadSeg
    hardware.py      the VIA and the IWM
    sound.py         the Sound Driver, the speaker and the Apple Sound Chip
    sound_manager.py the Sound Manager's channels and synthesizers
    timemgr.py       the Time Manager's tasks
    wdefs.py, mdefs.py, popups.py
                     applications' own WDEFs and MDEFs; pop-up menu controls
    controlpanel.py  control panels ('cdev' files)
    decompressors.py running 'dcmp' resources
    glue_*.py        the trap routines, a file a manager: quickdraw, cport,
                     color, gworld, palette, picker, pict, pictinfo, icons,
                     fonts, windows, menus, menucolors, controls, dialogs,
                     lists, textedit, scrap, events, help, resources, files,
                     aliases, stdfile, devices, desk, sony, cdrom, scsi, slots,
                     video, adb, sound, process, threads, aevents, editions,
                     components, ppc, print, sane, intl, script, utilities,
                     os, common

  toolbox/     the Toolbox's data and logic, apart from the traps
    api.py           the Toolbox calls as a guest application sees them
    system.py        the memory map and partitions
    memmgr.py        heaps, pointers and handles
    lowmem.py        low-memory globals and each application's World
    resources.py     the Resource Manager; sysres.py, the System's own resources
    templates.py     WIND, DLOG, ALRT, DITL, CNTL, MBAR, STR#
    quickdraw.py     GrafPorts, the QuickDraw globals, regions
    qdraw.py         QuickDraw's drawing
    pict.py, pictutil.py
                     pictures, played and recorded; the Picture Utilities
    screen.py        the screen buffer
    gdevice.py, color.py, pixels.py
                     graphics devices, pixel maps and colour tables; pixels as
                     host images
    icons.py, masks.py, cursors.py, bitfont.py
                     icons; CopyMask, SeedFill and CalcMask; cursors; bitmap fonts
    windows.py, menus.py, controls.py, dialogs.py, lists.py, textedit.py,
    scrap.py, events.py
                     the managers' records and logic
    files.py, aliases.py
                     the File Manager; alias records
    printing.py      the Printing Manager's records and its PDF printer
    sane.py, intl.py, script.py, numformat.py
                     SANE; the International Utilities; the Script Manager;
                     number formats
    constants.py, types.py
                     constants named as in Inside Macintosh; Point and Rect

  ui/          the Qt side
    display.py       where Mac windows and menu bars go on the host
    window.py        the widgets that show a WindowRecord
    chrome.py        window frames: geometry, hit-testing, painting
    menubar.py       the menu bar and menus, and MenuSelect's tracking
    nativemenu.py    the front app's menus in the host's native menu bar (macOS)
    tracking.py      TrackGoAway, TrackBox, DragWindow, GrowWindow
    bridge.py        running Toolbox calls on the GUI thread
    keymap.py        Qt keys to the Mac's key codes
    theme.py         colours, fonts and metrics the chrome shares
    stdfile.py, printdlg.py, colordlg.py, diskdlg.py, editiondlg.py
                     macrt's own dialogs: Open and Save, printing, the Color
                     Picker, which disk to insert, the Edition Manager's options
    audio.py         the Mac's speaker through the host's sound output
    fonts/           .ttf and .otf files to stand in for the Mac's fonts

tests/         the tests, tests/apps' 68k applications (docs/development.md)
tools/         gen_trapdefs.py
```
