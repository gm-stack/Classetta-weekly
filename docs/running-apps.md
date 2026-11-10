# Running a Mac application

[Go back to the documentation index](README.md).

Classetta runs classic Mac applications written for 68k Macs, targeting approximately System 1 to 7. Apps can run as windows on your desktop next to your OS's native programs.

It reads the application's resources and loads the 68k code into an emulated processor. Python answers the Toolbox and Operating System calls that code makes, and the graphical user interface is then drawn using PyQt6.

This page covers what to install, how to get an application to macrt in a
form it can read, how to run it, and what to do when it stops.

- [Quick start](#quick-start)
- [What you need](#what-you-need)
- [Which applications run](#which-applications-run)
- [Getting the application to macrt](#getting-the-application-to-macrt)
- [Running it](#running-it)
- [What happens at launch](#what-happens-at-launch)
- [What works](toolbox.md)
- [When it stops](#when-it-stops)
- [Surveying many applications](survey.md), on a page of its own
- [Writing a test application](development.md#the-test-applications)


## Quick start and command-line apps

<!--
TODO for this section:
  - What's the point of Hello.bin? We also seem to have `sampler` and `ticker`? Should we not just launch with no app specified so that we get those two instead?
  - Or perhaps the launch with no app should launch all three? If we've got it we may as well use it.

  There seems to be some redundant syntax in launching apps - the first example shows just running with `python -m macrt tests/apps/Hello.bin` whereas apparently below we need `--app` - check the source code to see what's really going on here.

  Some options feel a bit unnecessary - having more code to implement these means more code to test (and as this is a highly AI driven project, we are absolutely dependent on testing). Having more options presented to the user just makes the getting started seem overwhelming.
    - `--classic-menus` I don't think needs to be a thing?
    - I feel the same way about `--menubar window` - I feel like this just makes things way more complicated. But others may disagree.
    - `--rainbow` shouldn't exist IMO - we should just enable this automatically when colour is enabled, to indicate that it is.
    - `--single` feels really unnecessary. If this is used for testing, just pass a single `--app` instead.

-->

```sh
python -m pip install -e .
python -m macrt tests/apps/Hello.bin
```

Hello is a small test app in `tests/apps`, written in 68k assembly.
It is very basic - it has a window and a menu bar.

Then to run your own application:

```sh
python -m macrt MacPaint.bin
python -m macrt "System 6.0.8.dsk" --app TeachText
```

`pip install` will also puts a `macrt` command on your path, so `macrt
MacPaint.bin` does the same thing.

With no application specified, Classetta will launch a pair of demo apps.

### Application Options

`--app NAME`: Specify an application's file, or a path fragment (colon separated) that will be matched to the end of the path. The first candidate to match will be launched. Alternatively, specify a document to open it in its corresponding app (see [Opening a document](#opening-a-document)). Can be specified more than once to open multiple apps. If left unspecified, the "sampler" and "ticker" test apps will be launched.

`--list file`: Dump a list of a file's resources and exit. Does not run any code from the app.

`--view`: Launch an interactive explorer to show an application's menus, windows and dialogs (from its resources). Does not run any code from the app.

`--system FILE`: Uses a real System file at the specified path (or specify a disk image containing one). This is optional - Classetta has its own versions of many common resources, but in the event of compatibility problems, this can be tried.

`--memory K`: allocates K kilobytes of memory for the application. If unspecified, the "Preferred Size" will be used from the application's SIZE resource.

`--single`: With no `--app` specified, runs only `sampler` (and not `ticker`).

### User Interface Settings

`--host desktop`: (default) Each Mac window is a native window in your host OS. This works like Mac OS X's former "Classic" mode.
`--host screen`: Classetta will have single window representing the full Mac screen. This provides an interface like most emulators (qemu, Basilisk II etc...)

`--menubar global`: (default) Draw a single menu bar at the top of the screen.
`--menubar window` Attach a menu bar under each window's own title bar.
`--menubar native` Use the host's own native menu bar for the front application's menus. macOS only — the menus appear in the system menu bar at the top of the screen, and the emulated screen has no menu bar strip of its own.

`--classic-menus`: Disables implementation of Mac OS 8 style [Sticky Menus](https://dev.os9.ca/techpubs/mac/HIGOS8Guide/thig-55.html) - which allows menus to be clicked once and stay open. With this feature set, the mouse must remain pressed for the menu to remain open.

`--rainbow`: Shows a rainbow Apple logo in the menu bar.

### Emulated CPU

`--model plus`: (default) Emulate a Mac Plus - 68000 CPU and no FPU. Compatibility with earlier apps. If the screen is set to 512x342, it will be placed at the same memory address as the real machine.
`--model ii`: Emulate a Mac II - 68020 CPU, 68881 FPU. Some games will draw direct to screen memory if they detect a 68000 CPU, but will use better methods on later Macs. Supports colour!
`--model iix`: Emulate a Mac IIx - 68030 CPU, 68882 FPU. Also supports colour.

`--speed N`: Limit CPU speed to about N times the speed of a real Mac Plus (for compatibility with early games that don't use proper timing). Default is to run as fast as possible.

### Sound Options

`--no-sound`: Don't output sound produced by the applications. The sound hardware will still be fully emulated - `--record-sound` will still work with this option set. See [Sound](#sound).
`--record-sound FILE.wav` Writes the Mac's sound output to a WAV file.

### Display Options

`--size WxH`: The Mac screen's size in pixels, such as `512x342` or `1024x768`. Defaults to 640×480.

`--depth 1`: Black and White (default for Mac Plus)
`--depth 2`: 4 colours indexed / 4 greys
`--depth 4`: 16 colours indexed / 16 greys
`--depth 8`: 256 colours indexed / 256 greys
`--depth 16`: 'Thousands of Colours' mode (5 bits per channel)
`--depth 32`: 'Millions of Colours' mode (8 bits per channel)

`--row-bytes N`: Set a custom value for the low memory global `RowBytes`. Default will set it to the minimum number of bytes for horizontal resolution * depth.

> [!NOTE]
> Nearly all apps do not use the value set by `--row-bytes`. Real Mac OS typically does not change this value if display resolution/depth is changed after boot, so this value is unreliable on real hardware. Most apps that intend to draw directly to the framebuffer either assume based on hardware, or query QuickDraw to get a correct value. Adjusting this number might be needed for certain apps to draw correctly - although these apps will have issues on many real setups.

### Debug / Development

`--trace-traps`: Log every Toolbox/OS trap call to the terminal as it's made, with arguments and return value. Be warned: this will have significant performance impacts.

`--debug`: Runs with a debugger window that has a vaguely MacsBug inspired grammar. See [The debugger and MCP](debugger.md).
`--mcp`: Enable a MCP server for the debugger, allowing your AI agents to use it to assist in debugging. See [The debugger and MCP](debugger.md).
`--log FILE`: Writes all logged messages to a specified file (with any colour codes stripped).

## What you need

- **Python**: 3.11 or later.
- **PyQt6**: 6.5 or later - used to draw the UI.
- **unicorn**: 2.1 or later - used to emulate the 68k CPU
- **rsrcfork** and **machfs**: used to read resource forks and HFS filesystems.

(Classetta uses its own internal implementation for HFS+ and MFS disks.)

`pip install -e .` installs all dependencies. Unicorn has pre-built wheels for Linux, macOS and Windows, so compilation from source should not be necessary.

**To unpack Mac archives** (StuffIt, Compact Pro etc), get [The Unarchiver](https://theunarchiver.com)'s command-line tool, `unar`: `apt install unar` (Linux) or `brew install unar` (Mac OS with Homebrew).

**To run headless** (e.g. on a server or for CI tasks), set the environment variable `QT_QPA_PLATFORM=offscreen`.

### Fonts

The interface will draw with Chicago (system font) and Geneva (application font). These aren't bundled. If your system doesn't have them, you will need to put `.ttf`/`.otf` files in `macrt/ui/fonts/`.

Use and Modify has developed [ChicagoFLF](https://usemodify.com/fonts/chicago/), a freely licensed Chicago clone.

With an appropriate font present, text is drawn without antialiasing for the crisp 1-bit look.

Without one, Classetta falls back to using Helvetica at 13px with antialiasing on as it does not look good without it.

Set the environment variable `theme.TEXT_ANTIALIAS = True/False` to force anti-aliased text on or off.

An application can provide its own fonts in its resources, and these will be used.

See [Text and fonts](architecture.md#text-and-fonts) for more information.

### Menu Bars

**Wayland** doesn't let apps place their own windows. With `--host desktop`, windows can be repositioned but windows can't be placed by the application.

Use `QT_QPA_PLATFORM=xcb` to work around this (drawing via XWayland) or `--host screen` (drawing everything in one window).

**macOS** already has a global menu bar at the top of the screen. Use `--menubar window`, `--host screen`, or `--menubar native` (which puts the front app's menus in the system menu bar) there.

With `--host desktop`, the shaped windows and their 1-pixel shadows rely on `setMask` being correctly interpreted by the OS / window manager.

## What kind of applications are supported:

- 68k Mac application (`APPL` with `CODE` resources): fully supported
- Fat binary (68k and PPC): 68k version of the code will be used
- PPC only application (`cfrg` resources, no `CODE` resources): the viewer will be launched instead as Classetta does not (yet) support PPC emulation.
- A self-expanding application (AutoDoubler): will be decompressed by Classetta, and run as above.
- An application built using MPW's 'far code' model (`CODE` segments begin with `$FFFF`): supported, provided the entry point is not a far pointer.
- A desk accessory: can be launched from another application's Apple menu when another app is running, can't be run alone.

Control Panels are supported but do not appear as separate applications. Any `INIT` resources (that would normally be loaded at system boot) will be loaded on launch.

> [!NOTE]
> Many control panels are actually normal applications (type code `APPL`). True control panels have type `cdev`.

Extensions are not yet supported.

How well an application works entirely depends upon which Toolbox calls it relies upon (see [What works](toolbox.md) for more detail).

Programs that use widely used Toolbox calls and don't perform non-standard behaviour are most likely to work: word processors, utilities, and games that draw through plain QuickDraw.

Color QuickDraw is a bit more of a work in progress, as it is much more complex.

Games from the Mac's early years which attempt to draw directly to the screen buffer: run with `--host screen --size 512x342` and the display buffer will be placed into the emulated Mac's memory exactly where it would be on a real Mac to (hopefully) be compatible with this behaviour. See [Games from 1984](#games-from-1984) for more details.

> [!NOTE]
> TODO: confirm what happens with `APPC` type control panels.

### Tried so far

[Applications tried so far](titles.md) lists the software that has been tested so far.

If any unimplemented toolbox calls are made (that did not result in an immediate crash), at exit a list of unimplemented calls will be printed (see [When it stops](#when-it-stops) for more info / what to include with a feature request).

## Getting applications into Classetta

Unlike a lot of operating systems, classic Mac files can have two 'forks' or data streams. The **data fork** is the same concept as a file on nearly every other operating system. However, the **resource fork** holds individual entries of various types, in something more similar to a database. Segments of application code, menus, windows, dialogs, icons, sounds and strings are often stored as resources.

The file itself also has a four byte `type` code identifying what kind of file it is (application, document etc) and a four byte `creator` code linking the application and its documents - all without the use of file extensions. If this information is lost for an application, the classic Mac OS will not know it is an executable.

What this means is that if you copy a Mac application to any other operating system (including modern MacOS in some circumstances), or download it from somewhere that wasn't careful to archive it correctly, the resource fork can be lost. The application will then be unusable.

So the application needs to be provided in a form that correctly retains both forks. Classetta supports a number of the the common ones - just provide the file and it'll work out how to read it.

- MacBinary I, II or III - `App.bin` - a very popular format back in the day.
- BinHex 4.0 - `App.hqx` - also extremely common back in the day. Encodes as "plain text" so won't be corrupted by older online systems that were not "8 bit clean".
- AppleSingle  - one file holding both forks - used on Apple's A/UX and in some file archives, never widely adopted.
- AppleDouble - `App` (data) and `._App`/`.AppleDouble/App`/`App.rsrc`/`.rsrc/App`/`.finf/App` (resource) - `unar` extracting an archive or Mac OS copying to a filesystem that does not support forks (e.g. FAT32)
- Executor - `App` (data), `%App` (resource) - a similar format to AppleDouble
- Native MacOS - `App` / `App/..namedfork/rsrc` - macOS as host, a correctly copied app with resource fork intact
- HFS / HFS+ disk image - `.dsk`, `.img`, `.image`, `.hfs`, `.iso`, `.cdr`, `.toast` - raw images, DiskCopy 4.2 floppy images (800K and 1.4MB HFS, 400K MFS), HFS and HFS+ CD images, with or without partition tables. CD images can contain the full 2352-byte sectors.
- MOOF floppy image - `Disk.moof` - Applesauce format, imaging a 400K or 800K floppy's exact magnetic timings.

For AppleDouble and the emulator folders, you can specify just the data-fork variant filename (`App`). The corresponding Resource fork will then be automatically located.

### Checking a file

```sh
macrt Game.bin --list
```

`--list` prints the file's type and creator code, along with a list of every resource.

For menus, windows, dialogs, `SIZE` and `vers` resources it also decodes them. This provides a convenient way to tell that an application is usable - `CODE` resources likely indicate the app has 68k code to run.

```
$ macrt tests/apps/Hello.bin --list
Hello: application, type 'APPL', creator 'HELO'
  data fork 0 bytes, resource fork 1,007 bytes, 6 resources
  CODE      0       24  purgeable,locked,preload '' jump table, 1 entries
  CODE      1      460  purgeable          'Main'
  MENU    128       40                     Apple: About Hello…, -
  MENU    129       29                     File: Quit
  WIND    128       24                     'Hello', 260x160, procID 0
  SIZE     -1       10                     256K preferred, 128K minimum
```

In order: resource type, ID, size in bytes, attributes, name, and description of contents.

If Classetta says the file has no resource fork, the application will be unusable.

### Archives

Most Mac software online is distributed in an archive: `.sit` (StuffIt), `.cpt` (Compact Pro), `.sea` (self-extracting), `.sit.hqx`, or (uncommon for Classic Mac apps) a `.zip` with a `__MACOSX` folder.

macrt doesn't unpack archives. Unpack it with The Unarchiver, which will preserve the resource forks:

```sh
unar Game.sit.hqx               # decodes the BinHex and unstuffs what's inside
python -m macrt "Game Folder/Game"
```

On Linux and Windows, `unar` writes each resource fork, along with with the file's type and creator, as an AppleDouble file named `App.rsrc` next to `App`.

An application's data fork is usually empty, in this case `unar` may write only `App.rsrc`. You can point Classetta to App.rsrc. This shouldn't happen on macOS where it natively writes resource forks.

Don't use the command line `unzip` utility on a zip from a Mac: it leaves the resource forks in the `__MACOSX` folder, separate from their files. `unar` will extract the archive in a usable fashion.

### Disk images

Many collections come as disk images of floppies, CD-ROMs and hard disks. macrt can read MFS, HFS, HFS+ volume images directly, so these don't need prior extraction.

```sh
python -m macrt "example.dsk"                  # a disk with one application
python -m macrt "System 6.0.8.dsk" --app TeachText  # specify which app to run by name
python -m macrt Tools.img --app "Tools:Disk Tools:Disk First Aid" # specify an app by path.
python -m macrt Paint.dsk                           # a 400K MFS floppy from 1984
```

`--app` takes the application's name, or a colon separated path. Names will be matched as a suffix on any path, so a subfolder name can be specified, it does not necessarily have to start from the disk's root. Names are matched case insensitive.

If a disk with more than one application is specified without the `--app` parameter, you will get a list of all the applications on the disk.

```
$ python -m macrt mac6-0-8.dsk
  mac6-0-8.dsk holds 7 applications; pick one with --app:
  Mac System 6.0.8:Applications:Microsoft Excel:Microsoft Excel
  Mac System 6.0.8:Games:Creepy Castle:Creepy Castle™
  Mac System 6.0.8:Games:Operation Tsunami:Operation Tsuanmi
  Mac System 6.0.8:LabelWriter II Folder:LabelWriter II App
  Mac System 6.0.8:LabelWriter II Folder:TeachText
  Mac System 6.0.8:System Folder:PrintMonitor
  Mac System 6.0.8:TeachText
```

When two applications have the same filename, e.g. `--app TeachText`, Classetta will use the first one it finds (`Mac System 6.0.8:TeachText`). Specify a more complete path if you want the other one.

#### Image formats

Classetta can read raw images as well as DiskCopy 4.2 images.

Full-drive images including partition table (an Apple partition map with an HFS or HFS Plus partition) are supported.

Whole sector CD images (2352 byte sectors) are also fully supported.

Not supported: compressed images, particularly NDIF (DiskCopy 6) and UDIF (`.dmg`, `.smi`). You will need to convert these to an uncompressed raw image.

#### CD-ROM images

Images ending in `.iso`, `.cdr` or `.toast` will be mounted as a CD-ROM.

The driver has a device control entry in the unit table, named `.AppleCD`, for compatibility with apps (e.g. Lode Runner: The Legend Returns) which probe the driver to determine that it's a "real" CD-ROM. does.

The image is treated as the disc's single data track. Hybrid images with data and CD Audio are not supported.

The CD's volume will report that it's locked by hardware (as per Technical Note FL 530 "File Manager Volume Handling Q&As"). No modification of any files will be allowed.

An file with any other extension (`.img`, `.dsk`, `.image`, `.hfs`) that's not detected as an archive, will be treated as a floppy disk image if it is less than or equal to 1.4MB.

Above that size, it will be used as a hard disk. To the app there will be no reported driver in the unit table (unlike CD-ROM images).

#### MFS disks

MFS (Macintosh File System) predates HFS/HFS+ (Hierarchical File System). This is the filesystem used on 400K floppies under very early system versions (HFS was first provided with System 2.1. MFS was made read-only in Mac OS 7.6.1 and dropped entirely with Mac OS 8). The first MacPaint and MacWrite disks and most early games were shipped on disks in this format.

MFS does not actually support paths / hierarchies. Although the Finder will show files in folders, this is actually an illusion created by the Finder itself - it stores the path of each file in the Desktop file. Applications tended not to read this and just present every file on disk in a flat list.

#### Disks that start up into a game

A few early games shipped a bootable system without a Finder. A Mac started off that disk would boot directly into the game.

If such a disk image is provided, and a non-Finder application is specified in the disk's Boot Blocks, Classetta will start this application without needing to specify `--app`.

#### Floppies, track by track

Classetta can also read Applesauce MOOF images of 400K and 800K floppies. These preserve each track as the raw magnetic flux transitions the drive reads. The moof-a-day collection keeps its floppies in this format - Classetta will decode them back to sectors, in the same way a real Mac's disk drive would. A sector that can't be correctly interpreted will be loaded as all zeros, and a program that uses the normal floppy disk calls will receive a read error.

A few MOOF images preserve invalid magnetic data - Classetta interprets them as close to a real Mac's floppy controller as it can.

MOOF images of 1.4MB floppies are not supported - convert them to a raw disk image first.

A raw or DiskCopy image of a 400K or 800K floppy has only the data from sectors, without any information as to where track boundaries would lie. Classetta determines what track they would have been present on as best it can. A program that performs low level reads of the disk should find what it's expecting to see.

Why is this useful? Here are the scenarios that have been tested:

- Attempting to read directly from the floppy disk controller should always give an identical result to the original.
- Some apps such as Excel 1.0 modify the OS's disk read routine or use it to read other information on disk.
- Some apps try to determine that they see identical timing (rotation speed) to an original Mac floppy drive - these should see what they expect (Mac Attack).
- When the track has no flux change recorded for greater than the maximum allowed interval, a real drive will start to read random bits in that location. Some applications are deliberately mastered with a disk that has this on some sectors. The app will try reading this sector multiple times to check whether it differs on each read (Q-Sheet).
- Some alter the bit spacing on the disk and check the timing changes between this and other parts of the disk (The Ancient Art of War).
- Some apps will step the head to another track mid-read and examine the byte stream to determine how the tracks are physically aligned with each other (PageMaker, Jazz).
- Some apps read the raw bits from the floppy controller as they pass under the read head (Shufflepuck Cafe). The bits will go by at the correct speed.
- An app with its own routines to find a sector's address field (Dragonworld) will be correctly called for each sector the driver reads.
- A key track whose flux changes are 1.5 bits apart will also read differently each time (Fraction Action, MathTalk and KidTalk).
- Some apps the head directly through the drive's registers, and then wait using the WakeUp call in the ROM, expecting a specific sector to follow (Moebius).
- Some apps check that disk step/settle times are reasonable (Classetta emulates 12 and 30 ms).

Some apps that do more intensive checks on the floppy drive's hardware may still fail, with a few programs reported as showing a "damaged disk" or "not an original" alert.

### Where to find software

Please run only software you have the right to run.

Apple's own system disks, such as System 6.0.8, are a good first test: they include TeachText and a few small utilities.

The Info-Mac archive, mirrored at `https://ftp.funet.fi/pub/mac/info-mac/`, holds thousands of old freeware and shareware programs as `.hqx` files.

Small black-and-white programs are most likely to be compatible.

The Internet Archive keeps images of many original 400K floppies, as raw `.dsk` or DiskCopy images as well as MOOF images in the moof-a-day collection of about 150 early Mac titles.

Its Macintosh Software Library holds many more, as DiskCopy images (`.dc42`, `.img`) or raw `.dsk` files. Where an item has several options, the one named `00playable` is the best option.

## Keyboard

<!-- TODO for this section: validate the part about keyboards. Not sure it would be detecting a Mac keyboard so much as the host being Mac OS? -->

When using a PC keyboard, **Ctrl** is mapped to Command (⌘), **Alt** is mapped to Option, and Windows/Super is Control. On a Mac keyboard, modifier keys are left as-is.

There are some keys which differ per model:

The Mac Plus's keyboard, which pre-dates ADB keyboards sends arrow keys are $46, $42, $4D and $48 (which are identical to keypad +, *, / and = with Shift held down). On the other hand, other Mac models have an ADB keyboard which sends arrow keys as $7B to $7E.

Currently this discrepancy is handled by setting both keycodes at once.

### Several applications at once

A program that ejects a floppy gets it back a second later, as if you had
put it back in. The drive then posts a disk-inserted event, and the disk
is mounted again. A game that ejects its disk and waits for a disk, as
Seven Cities of Gold does for its map disk, goes on with the same disk.

A game on more disks than there are drives ejects a disk, unmounts it
(`_UnmountVol`) and asks for another. Then a dialog asks which of the
disk images you named goes in that drive, the next one in turn chosen.
Insert puts in the disk you choose, and Put Back puts back the disk that
came out. If the disk you choose is in another drive, it comes out of
that drive, and the disk it replaces goes in there, unmounted. Without a
display to ask on, the next disk in turn goes in. Wizardry VI is played
this way:

```sh
python -m macrt A.moof B.moof C.moof D.moof E.moof F.moof --app Bane --host screen
```

The applications run side by side, as under MultiFinder. Each has its own
memory partition, A5 world, low-memory globals and menu bar. The menu bar
belongs to the front application, and clicking another application's
window brings that application to the front.

An application that launches another one (with `_Launch`) gets it the same
way. A `_Launch` without `launchContinue` replaces the application, as on a
Mac. System 7's `LaunchApplication` works too: its parameter block names
the application with an `FSSpec`, and gets back the new process's serial
number and the partition sizes from its `SIZE` resource.

### Opening a document

`--app` can name a document on a disk image instead of an application.
macrt then launches the application that made it, the one whose creator
the document has, from the same disk if it's there or else from the other
images, and opens the document with it, as double-clicking the document
does in the Finder. Mac Pro Football's game is a document of this kind:
a program for Microsoft BASIC's run-time, which is the application.

```sh
python -m macrt "Mac Pro Football.dc42" "Team Files.dc42" --app MSFL --host screen
```

The application finds the document in its Finder information, the handle
that `_CountAppFiles` and `_GetAppFiles` read (also at 16(A5)), with the
message to open it rather than print it. Each document is listed with a
working directory for the folder it's in. A System 7 application that
handles Apple events gets an Open Documents event for it instead of Open
Application.

A HyperCard stack is a document of HyperCard's. The Manhole's disk has
only its stacks, so give HyperCard's disk after it:

```sh
python -m macrt TheManhole.img Culture.img --app "the Manhole" --host screen
```

Documents made by the same application, named one after another, open in
one launch of it, as when they're selected together in the Finder. An
`--app` naming the application itself is a launch of its own, with no
documents, unless documents follow it: a document whose creator is an
application named before it opens in that one. This picks which of two
HyperCards opens a stack:

```sh
python -m macrt StrangerThings.img HypercardBootVolume.img \
    --app "HyperCard 2.4:HyperCard" --app "Stranger Things" --host screen
```

### Documents and saving

An application starts in its own folder, as when the Finder opens it, and
its Open and Save dialogs start there too. These are macrt's own dialogs,
not the application's. Where a file it saves ends up depends on where the
application came from:

| The application came from | What happens to files it writes |
| --- | --- |
| a folder on your computer | They're written into that folder. A file read as MacBinary or AppleSingle is written back the same way. Any other file is written as its data fork under its own name. Its resource fork, type and creator go in the side file it already had, such as `unar`'s `name.rsrc`, or for a new file in an AppleDouble `._name`. macrt reads all of these back the next time. Symbolic links in the folder aren't followed: applications don't see what they lead to, and nothing is read or written through one. A side file that isn't the file's own, such as a `name.rsrc` that is a file of its own, is left alone. |
| a disk image | The changes are kept in memory and lost when macrt quits. The image file itself is never changed. A CD's image takes no changes at all (see [CD-ROM images](#cd-rom-images)). |

A BinHex file is text, not a Mac file, so macrt treats it as locked. Some
applications keep their settings in their own resource fork, and can't
save them while they run from a `.hqx`. Unpack it with `unar` first.

Applications also see a startup disk called **Macintosh HD** with a System
Folder. Preferences files they write there last only until macrt quits.

### Printing

macrt's printer prints to a PDF. Page Setup and Print show macrt's own
dialogs rather than a printer driver's:

- **Page Setup** picks the paper (US Letter, US Legal or A4) and whether
  the page is tall or wide.
- **Print** picks all the pages or a range of them, the number of copies,
  and the PDF to save. It's named after the front window, in the folder
  you started macrt from (`Untitled.pdf`, or `Untitled 2.pdf` if that's
  taken). Choose… picks another place.

When the PDF is written, macrt says so:

```
  printed 2 pages to /home/you/Untitled.pdf
```

The printer says it's an ImageWriter that prints at 72 dots per inch, so
every application draws its pages with QuickDraw, as it does on the screen.
The PDF's pages are black and white at 288 dots per inch. Applications from
1984 find the printer through their own glue code, as they did on a Mac
(see [What works](toolbox.md)). MacPaint prints a bitmap at a time,
straight to the printer driver, and shows no Print dialog. Its PDF gets the
name the dialog would have given it. The printer is the same with a real
System file (`--system`), whose own printer drivers would drive a serial
port.

### A real System file

Without `--system`, applications run with macrt's own small System file.
It has what applications most often ask a System file for: the standard
window, menu and control definitions, the packages (`PACK` 0 to 15), the
stop, note and caution icons (`ICON` 0 to 2), the standard cursors
(`CURS` 1 to 4: I-beam, cross, plus and watch) and the US international
resources (`INTL` 0 and 1). It also has the pattern list (`PAT#` 0) that
paint programs show as their palette, and the desktop's gray and light
gray (`PAT ` 16 and 17). Its icons, cursors and patterns are
drawn for macrt. They're kept in memory, not in a file, but as with a
real System file's, one a program releases is read in again when it asks
for it: Aladdin Activity Centre releases the watch cursor and gets it
again with `GetCursor`. Some applications look for resources that only a real
System file has, such as other icons, strings (`STR#`), the fonts
themselves and the Script Manager's `itl` resources.

When the applications come from a disk image that has a System file of
its own, macrt's System gets that file's fonts of the families it hasn't
got, up to 128K of them: programs drew in the fonts of the System they
started up from, and some brought their own there, as Mac Attack's tanks
and landscape are characters in fonts of its disk's System. Fonts of the
standard families stay macrt's, but the monospaced ones, Monaco and
Courier, are measured with the widths of that System's strikes of them,
as with a real System file (below): Hacker types each letter in Monaco 9
and its cursor after it, then backs up six pixels, Monaco's width, to
type the next letter over the cursor. And lines in a standard family
are as far apart as that System's strikes of it set them: `GetFontInfo`
gives their ascent, descent and leading rather than those of your
computer's font, a 16-pixel line for Chicago 12, so that The Venice
Project's prompt goes under the box it asks you to type in, not across
it.

It gets that System's drivers too, those named with a period, such as
a speech driver, and the resources they load that aren't of the kinds a
System file of Apple's has, up to 128K: Speller Bee speaks its words
through the `.SAY` driver its disk's System has, which loads its code
from `OVLY` resources there.

And it gets that System's decompressors, its `dcmp` resources. Many of
System 7's resources are compressed, as System 7.5's utilities' are:
macrt expands those it can itself, and for the others runs the `dcmp`
that the resource's header names, on the 68k, as the Resource Manager
did.

The System version that Gestalt, `SysEnvirons` and the low-memory
`SysVersion` give is that System's, from its `vers` resource, or a real
System file's, given with `--system`, from 7.0.1 to 7.6.1: System 7.5's
utilities look for 7.5. Without one, or with an earlier System, it's
7.0.1.

For everything else, give them a real System file:

```sh
python -m macrt Game.bin --system "System 6.0.8.dsk"
python -m macrt Game.bin --system "System Folder/System"
```

The System file's resources then sit at the bottom of every
application's resource chain, as on a Mac, and its desk accessories are
in the Apple menu. Chicago, Geneva and the other standard fonts are
still drawn with your computer's fonts, but measured with the widths of
the System file's own bitmap fonts, at the sizes it has them, so that
text lines up as it did on a Mac. The fonts in the suitcases in the
Fonts folder beside the System file are the System's too, up to 192K of
their bitmap fonts (not their TrueType outlines): System 7.5's System
file has Geneva only at 9 and 12 points, and Courier and Helvetica only
in that folder, which HyperCard Player's stacks ask for. (A standard
font the application carries in its own file, at the size asked for, is drawn from its
bitmap, as Balance of Power's New York is, and Gemstone Warrior's
Chicago, which it makes the application font by setting `ApFontID`. An
application's own Chicago 12 is the one its menus are drawn in, as on a
Mac: The Fool's Errand's menu titles are Roman numerals in its font.)
Its 68k definition procedures and packages aren't run: windows, menus and
the standard controls are always drawn by macrt, and the package calls
are always macrt's. Anything above
that the real System file lacks, macrt adds.

The Finder beside the System file, if there is one, is in the System
Folder with it, where a program that opens it by name finds it from its
own folder, and changes to it stay in memory, as the System file's do:
FinderColors, from BMUG's PD-ROM Volume 2, opens it to read and change
the colours of the Finder's Color menu. macrt doesn't run the Finder;
applications start as `--app` names them.

A System 7 file's System heap has room to grow, as a real one's has
("the system heap expands dynamically to meet your request", Inside
Macintosh: Memory, "Allocating Memory at Startup Time"): up to 1.5 MB,
with applications' partitions above it, where macrt's own System keeps
to 512K. HomePublisher marks its code segments to be loaded into the
System heap, and couldn't start in 512K.

### Control panels

A control panel, a `cdev` file in the Control Panels folder, runs as
System 7's Finder ran one: in a window of its own, with the panel's code
called for whatever happens there (Inside Macintosh: More Macintosh
Toolbox, Control Panels). Name it with `--app`, from a disk with
System 7 on it:

```sh
python -m macrt "System 7.5.3.img" --model ii --depth 8 --size 640x480 \
    --app "System Folder:Control Panels:Date & Time"
```

The window is made of the rectangles in the panel's `nrct` resource, its
text in the font its `finf` names, or in 9-point Geneva, the application
font, without one. Its close box, File › Close or
File › Quit closes it, and the Edit menu's commands go to the panel. A
panel whose `mach` resource says to ask it whether it can run on this
Macintosh is asked, and one that says no isn't opened: macrt says it
"can't be used with this Macintosh".

A panel with `INIT` resources of its own, the code a Mac runs from it as
it starts up, has them run first, each called as at startup (Inside
Macintosh IV, The System Resource File, "Initialization Resources").
One that stops with a system error is left out, as if it hadn't been
loaded: the traps it patched are put back, and what it gave Gestalt
goes. If the panel then stops as it opens, or later in code its `INIT`
resources left behind, it's opened again without them, as on a Mac
started with its extensions off, and macrt's log says so. Traps an
`INIT` patched with code in the system heap stay patched for the
applications launched after; a patch in the panel's own memory goes
when the panel closes. Extensions that are files of their own aren't
run.

From System 7.5.3's disk, with the options above:

| Control panel | What happens |
| --- | --- |
| Color, Date & Time, Easy Access, Extensions Manager, General Controls, Keyboard, Map, Memory, Mouse, Numbers, Sound, Startup Disk, Text | Run. Sound lists the alert sounds, and its pop-up menu switches to its other views, which are components in its own file (see [What works](toolbox.md)). Startup Disk lists the disks. |
| Apple Menu Options, CloseView, WindowShade | Run, their `INIT` resources run first, and open to their settings. |
| AutoRemounter, Control Strip, Macintosh Easy Open, PC Exchange | Open, but each says first that it isn't installed: their `INIT` resources call on parts of the System that macrt hasn't got, and stop, so the panel runs without them. |
| Speech | Opens, but says it needs text-to-speech or speech recognition software, which macrt hasn't got. |
| Launcher | Opens its window, with nothing in it. It shows what is in the Launcher Items folder of the System Folder in use, which is macrt's own on Macintosh HD, not the disk's, so it makes an empty one. |
| 601 Processor Upgrade, Auto Power On/Off, Brightness, Monitors, Password Security, PowerBook, PowerBook Display, PowerBook Setup, Power Macintosh Card, Screen, Serial Switch, Trackpad | macrt says each can't be used with this Macintosh, a Mac II with macrt's screen. |
| File Sharing Monitor, Labels, Sharing Setup, Users & Groups, Views | Refused: they have no code of their own for macrt to run. |
| Desktop Patterns, Energy Saver, Monitors & Sound | Aren't `cdev` files. Desktop Patterns is an application, and runs as one (see [Tried so far](titles.md)). |

A control panel on a disk of its own runs the same way. ScreenChooser 3.1
(`ScreenChooser.img --app ScreenChooser`, with the options above) shows
its shareware notice, and OK opens its General Options: its check boxes
check and uncheck, its Quick Help says what each is for, and Help shows
its user's guide. Backdropper and Set Backdrop, beside it, say
ScreenChooser isn't running: they're programs of their own, and
ScreenChooser's `INIT` resources run only as its control panel opens.

After Dark 2 (`AfterDark.img --app "After Dark"`, with the options
above) opens with its modules listed, Flying Toasters picked. Demo
shows the module, its toasters flying across the screen, and a click
brings the panel back.

After Dark 3.0 (`AfterDark32.img --app "System Folder:Control
Panels:After Dark 3.0"`, with the options above) says first that it
can't run, that it needs System 6.0.7 and MultiFinder: its `INIT`
resources look for MultiFinder by asking `GetTrapAddress` for
`_OSDispatch`, which macrt says isn't there (see [What
works](toolbox.md)). Its panel runs all the same. It says the copy is
unregistered, and OK opens it with its modules listed, Flying Toasters
Pro picked. Demo shows the module, its toasters flying across the
screen around the panel's settings. The modules' libraries go in
temporary memory.

After Dark 4.0's demo disk (`AfterDark40.img --app "System
Folder:Control Panels:After Dark 9" --memory 4096`, with the options
above) says first, for the same reason, that it needs System 7.0 or
later, and its panel opens with its modules listed. Demo launches the
application in the panel's own file, and the panel quits, as a program
that calls `Launch` does. Flying Toasters needs 256 colours, and about
3.3 MB for its toasters, which it takes from the application's heap
when there's no `_OSDispatch` for temporary memory. That application's
`SIZE` resource asks for 600K, and the module says it's out of memory,
so `--memory` gives the file a bigger memory size, as Get Info would.
With it, the toasters fly, the words of their song along the bottom of
the screen, with the module's settings in front.

The Simpsons Screen Saver's disk (`AfterDarkSimpsons.img --app "System
Folder:Control Panels:After Dark 9"`, with the options above) has
After Dark 4.0's panel with modules of its own, in the Simpsons folder
of the panel's pop-up menu. Their library takes temporary memory
through `_OSDispatch`, so they run without `--memory`. Demo shows
Chalkboard's lines being written on the board, Lisa's Mood Swings'
Lisa playing her saxophone, and Itchy and Scratchy on a desktop of
their own. That desktop's folders and Trash are the System's icons:
with `--system` given the same disk they show, and with macrt's own
System only their names do. Itchy & Scratchy asks for 2 MB of
temporary memory at once. With `--memory 4096` there's less than that
outside the panel's partition, and its screen stays black. The
folder's other modules run too: How to Draw sketches Maggie, Homer
Eats eats the desktop from the bottom up, Physics bounces the family's
heads, Simpsons Trivia asks its questions, and Snowball I floats on
clouds with a halo.

### Games from 1984

The first Macintosh games often go around the Toolbox. They draw
straight into the screen, read the mouse button from the hardware, and
keep time by the processor's speed or by the VIA's timers.

macrt gives them what they reach for: the screen buffer at `ScrnBase`, a
Mac 512K's screen where a 512K has it, the VIA, the exception vectors,
the floppy disk driver and the drives, and the Event Manager's
low-memory record of the mouse and keyboard. [The
hardware](toolbox.md#the-hardware) says how each one works. `--speed`
holds the processor to about a Mac Plus's pace, for games that time
themselves by how fast it goes.

So a game on a 400K floppy runs like this:

```sh
python -m macrt "Mac 128K Games Vol. 1/Frogger.DSK" --host screen --size 512x342 --speed 1
```

A game that doesn't use menus may draw over the menu bar, in the top 20
rows of the screen. As on a Mac, the bar is in the screen buffer, so what
the game draws there, white or black, shows over the bar until it calls
`DrawMenuBar`. A window that reaches up over the bar doesn't draw there,
as its visRgn leaves out the bar, as the desk (`GrayRgn`) does. A game
that means its window to cover the bar puts the bar back in the visRgn
itself, as Indiana Jones and the Last Crusade does, and then what it
draws there shows over the bar in the same way. A game's windows go on
showing the main screen buffer while the VIA shows the alternate one.

### Sound

A Mac 128K, 512K or Plus makes its sound from a buffer in memory, at
`SoundBase`: at each tick, 370 samples from it go to the speaker, while
the VIA has the speaker on, at the volume in the VIA's low bits. macrt
plays the buffer the same way, through your computer's sound output.
The volume starts at 7, the loudest, in the VIA, in `SdVolume` and in
parameter RAM. What's in the buffer comes from three places:

- **The Sound Driver**, `.Sound`, reference number -4. `StartSound`, a
  `Write` to it, plays a sound through one of its three synthesizers:
  square-wave, tones of a pitch, a loudness and a length; four-tone,
  four waveforms played together, as MusicWorks plays its songs; and
  free-form, samples at a rate. An asynchronous sound plays while the
  program goes on, those after it waiting their turn, and its completion
  routine is called when it's done. `StopSound`, a `KillIO`, stops them.
- **The Sound Manager** (Inside Macintosh V), which System 6 and 7
  programs call: `SndPlay` plays a `'snd '` resource, and sound channels
  take commands from the program, `SndDoCommand` queueing them and
  `SndDoImmediate` sending them at once. At each tick, each channel's
  synthesizer makes the next frame, and they're added to the Sound
  Driver's. The note synthesizer plays notes, from a sine to a square
  wave as the timbre goes up; the wave table synthesizer plays the
  program's own waveform; the sampled sound synthesizer plays sound
  headers' samples (`bufferCmd`), and notes on an instrument from one
  (`soundCmd`), looped while the note lasts. `waitCmd`, `callBackCmd`,
  `pauseCmd`, `ampCmd`, `rateCmd` and the rest are there, and so are
  modifiers, the program's own, from `SndAddModifier`. `SndPlay` with no
  channel, or not asynchronously, waits for its sound to end, as does
  `SndDisposeChannel` without `quietNow`, with the ticks going on.
  MineSweeper, Glider, Glypha, Shadow Keep and Tesserae play their sounds
  this way. Play from disk, `SndStartFilePlay`, plays an AIFF or AIFF-C
  file the program has open, or the sampled sound of a `'snd '`
  resource, all of it or the seconds its audio selection says;
  `SndPauseFilePlay` pauses and resumes it, `SndStopFilePlay` stops it or
  waits for its end, `SndChannelStatus` gives its times, and the
  program's completion routine is called when it ends, however it ends.
- **The program itself**, writing its samples into the buffer at each
  tick, as Dark Castle, Crystal Quest and SmoothTalker's speech do. A
  program needn't start at the buffer's first word: counting from 0,
  Creepy Castle starts at word 10, Prince of Persia at 37, Dark Castle
  at 40, Crystal Quest at 42 and Tetris at 140, each going round to just
  before where it started. macrt plays each frame from where the program
  starts writing it, which it looks at twice a second, by watching one
  tick's writes to the buffer.

On a Mac II, a program can also write its samples to the Apple Sound
Chip, at the address in `ASCBase`, as Digital Messiah does with its
music, 370 of them at each tick from its VBL task. What it writes to the
chip's first 1K goes into one queue of samples and its second 1K into
another, each keeping up to 1K; at each tick the speaker plays the next
370 from them, the two averaged while both have some, with the sound
buffer's. macrt's Mac Plus has no chip, and its `ASCBase` is 0.

`--no-sound` keeps the sound off your speakers, and `--record-sound
FILE.wav` writes it to a WAV file as well: 8-bit mono, 22,200 samples a
second. A sound output that doesn't take 22,200 samples a second gets
them at its own rate, each of its samples on the line between the two
of the Mac's it comes between.

How it differs from a Mac:

- macrt's ticks are 60 a second, a Mac's 60.15, so the samples play at
  22,200 a second rather than 22,255, and the Sound Driver's tones are 4
  cents flat.
- The ticks, and so the sound, go on while the application's code runs
  and while it waits: in `Delay`, in `WaitNextEvent` with time to sleep,
  in a menu, a modal dialog or a window being dragged. Its VBL tasks and
  Time Manager tasks run at those ticks too, but the VIA's interrupts
  wait for the program's own code to run again, since they would come in
  the middle of the call it waits in. Under MultiFinder, the ticks that
  run are those of the application that has the processor, or had it
  last, each application's VBL tasks being its own.
- When the host is late for some ticks, as it may be when it's busy, the
  VBL tasks and the sound make them up all at once, up to a third of a
  second of them. After another application's turn, or a time with
  interrupts masked, they make up a tenth of a second at most, and after
  a tick that left no VBL task counting down and no sound to make, none:
  a task installed then counts from its first tick. A longer gap is one
  tick.
- The alternate sound buffer isn't played, and timer 1 doesn't turn the
  speaker on and off by itself, as some programs have it do through the
  VIA.
- `SysBeep` is your computer's own beep. MIDI, which goes out through
  the serial ports, plays nothing.
- The Sound Manager's synthesizers stand in for Apple's `'snth'`
  resources, which aren't run, and nor are modifiers that are resources.
  Its sounds are the Plus's, in one channel, so stereo's sides are added
  together; compressed sounds are silent, as is what `Exp1to3` and
  `Exp1to6` expand MACE to, and `syncCmd` doesn't wait for
  other channels. A channel's commands past its full queue wait outside
  it, so a program never waits for room.
- Up to 64 channels can be open at once, a limit of macrt's own; past it
  `SndNewChannel` returns `notEnoughHardware` (-201). DeadEnd counts the
  channels it can have by asking for one more until it's refused.
- Play from disk reads the whole sound from the file when it starts,
  rather than a buffer at a time through the program's buffer.
- The Apple Sound Chip is macrt's own: where it is, the two queues and
  what the speaker takes from them are what Digital Messiah needs, not
  taken from Apple's description of the chip. What a program writes to
  the rest of it, from $800, is kept and reads back, but isn't heard: a
  volume or a way of playing it sets there makes no difference.


## What happens at launch

1. **Memory.** macrt reads the application's `SIZE` resource (or, without
   one, the sizes from the Finder's Get Info) and gives it a partition of
   that size in an 8 MB Mac, and 24K more for its bigger stack (see [The
   stack](architecture.md#the-stack)). `--memory` gives it a size of your
   own. An application that asks for more than is free is refused, and
   macrt says how much it needs.
2. **The A5 world.** `CODE 0` describes the application's globals, which
   sit below A5, and the jump table, which sits above it. macrt builds them
   as the Segment Loader did and loads `CODE 1`. The other segments load
   when the code first calls into them.
3. **The processor.** unicorn's 68020, in 24-bit mode, starts at the
   first jump-table entry. Gestalt, `SysEnvirons` and the low-memory
   globals say it's a Mac Plus, with a 68000 and no floating-point unit,
   and the exception frames it stacks are a 68000's (see `--model`).
   The few instructions of a 68020 and a 68881 that unicorn doesn't run,
   or runs otherwise than Motorola's manual says (`CHK2` and `CMP2`,
   `PACK` and `UNPK`, packed decimal `FMOVE`, an `FMOVE` of immediate
   data to the floating-point control registers, `FSAVE` and `FRESTORE`)
   stop it, and macrt runs them in Python (`macrt/m68k/instructions.py`).
4. **Traps.** Every A-line trap the code executes (`_NewWindow`,
   `_DrawString`, `_WaitNextEvent` and so on) stops the processor. The
   matching Python routine then reads its arguments from the 68k stack or
   registers and does what Inside Macintosh says that routine does.
   - Windows and menus become Qt windows and menus.
   - QuickDraw draws into an image for each window, which Qt shows.
   - The Mac's data structures (window records, handles, regions and so
     on) are real, in the Mac's memory, so code that reads them directly
     still works.
5. **Events.** Mouse clicks, keys, and update and activate events come from
   Qt and are queued for `GetNextEvent` and `WaitNextEvent`. An application
   waiting in `WaitNextEvent` lets the others run. One whose `SIZE`
   resource says it takes high-level events finds an Open Application
   Apple event waiting, as System 7's Finder sends when it opens an
   application without documents.

[How macrt works](architecture.md) goes through each of these in more
depth: the processor, the trap dispatcher, memory, drawing and resources.


## What works

macrt answers more than 700 Toolbox and Operating System routines.
[What works](toolbox.md) goes through them a manager at a time, from the
Memory Manager to the Sound Manager, and lists what isn't there yet.


## When it stops

### A call macrt doesn't implement

```
  MyApp: SetDeskCPat (trap $AA47) isn't implemented; skipped
```

The routine is known but has nothing behind it yet: here `SetDeskCPat`,
which sets a colour desktop pattern. A routine that comes through a
package or dispatch trap (`Pack3`, `QDExtensions` and the rest) is named by the
routine its selector picks rather than the trap. macrt takes its
arguments off the stack and returns 0, or `unimpErr` (-4) for a routine
that returns an error code. Many applications carry on regardless, so each
missing call is reported only once. When the application quits, macrt
counts them up:

```
  MyApp called traps macrt doesn't implement: SetDeskCPat (1), HMGetHelpMenuHandle (1)
```

That list is the most useful thing to report: it says exactly what an
application is waiting for.

### A system error

When the application does something it can't recover from, macrt stops it
and shows the classic system error alert. The other applications keep
running. The terminal gets the details:

```
  MyApp stopped: system error 3 (illegal instruction): at $00002A
  PC 0000002A  SR 2000
  D0-D7 00000000 0000000C 00000000 00000000 00000000 00000000 00000000 00000000
  A0-A7 00000000 00084A10 00000000 00000000 00000000 001840F8 0017FE40 0017FE2C
  last traps: GetNextEvent at $08F2A4, …, Get1Resource at $0899C2, HLock at $0899CA
```

The first line says what went wrong. Here, MyApp jumped to $00002A, in
low memory, which isn't code (see the table below). Below it are the
registers and the last 16 traps, newest last, with the address each was
called from.

When the first line only names an exception, the last traps usually show
the cause. Often a call shortly before was skipped, and the application
went on with a value that call should have filled in.

| ID | Name | What it usually means here |
| --- | --- | --- |
| 0 | macrt itself failed | A bug in macrt, not in the application. A Python traceback comes with it. |
| 1 | bus error | The application read or wrote memory outside the Mac's 8 MB, or passed a bad pointer to a trap. Usually it's a handle from a call that failed earlier. |
| 2 | address error | It jumped to an odd address. |
| 3 | illegal instruction | It ran into data: often a call through a NIL procedure pointer, or a segment that isn't code. |
| 4 | zero divide | It divided by zero, often by a value a skipped call should have set. |
| 5 | check trap | A `CHK` instruction failed (Pascal range checking). |
| 6 | overflow trap | A `TRAPV` instruction failed. |
| 7 | privilege violation | It ran an instruction that isn't allowed. |
| 8 | trace trap | The trace bit was set. |
| 10 | line 1111 trap | An F-line instruction that the 68020 and its 68881 don't have, such as an MMU instruction. Floating-point instructions themselves work, whichever `--model` it is. |
| 11 | miscellaneous exception | Any other processor exception. |
| 12 | unimplemented trap | A trap macrt doesn't even know the name of, or a private call. PrintMonitor's private MultiFinder calls stop it here. |
| 15 | segment loader error | A `CODE` segment that's missing or compressed, or one built for the far code model that the application's own runtime doesn't load. |
| 25 | out of memory | A Toolbox call couldn't get the memory it had to have. Most calls report `memFullErr` instead and let the application decide. |

### Looking closer

- `--trace-traps` logs every trap as it's called: its arguments, what it
  answered, and the address it was called from -- the live address and,
  when the application's dump is known, the same byte in Ghidra's
  `ram:` image. Use it to see what an application was doing before it
  stopped, or why it's sitting still. A package call is logged with the
  routine it's for, by Apple's name:

  ```
  F/A-18 Hornet: trap GetNewWindow($C048, $59F16A, $FFFFFFFF) -> $5F9250 (called from $192E0C / ram:00013A72)
  Fly Swatter: trap FP68K FMULS(?) -> $40000000 (called from $1033F6)
  Microsoft Excel: trap IUMagString(?) -> patched $5A2110 (called from $1193A6)
  ```
  A trap's arguments show when the interfaces describe them; a `@raw`
  package or a patched trap shows `(?)` or `-> patched $...` when the
  answer is the application's own code's to give.
- `--view` shows the application's menus, windows and dialogs from its
  resources without running any code. If you mainly want to see what an
  application looked like, this always works. An application with no 68k
  code gets the same. `viewer.py`'s ResourceViewer does what the
  application's startup would do, through the same Toolbox calls:
  `GetNewMBar` or `GetMenu` for the menu bar, then `GetNewWindow` for the
  first `WIND`. A *Resources* menu opens the other windows, dialogs and
  alerts.
- `--list` shows what the application is made of. An `ALRT` or `STR#`
  near the error often says what the application was trying to tell you.
