#!/bin/sh
# make.sh -- build both LisaSerialFtp clients on a PC with the Lisa Pascal
# cross-compiler (https://github.com/TorZidan/LisaPascalCrossCompiler: the
# Workshop 3.0 Assembler, Pascal compiler, Linker, Alert phrase generator and
# InstallTool, writing the same files as the Workshop's):
#
#   LisaSerialFtpClient  the Lisa Workshop client (LisaSerialFtpClient.text,
#                        with LisaSerialFtpClientInclude.text)
#   LosSerialFtpClient   the Lisa Office System client, tool 197001
#                        (LosSerialFtpClient.text, LosSerialFtpClientAlerts.text)
#
# Both include the shared code with the I directive: LisaSerialFtpDecl.text,
# LisaSerialFtpUtil.text, LisaSerialFtpSerial.text, LisaSerialFtpProto.text,
# LisaSerialFtpXfer.text, LisaSerialFtpCmds.text; and both link the same 68000
# routines (LisaSerialFtpClientAsm.text; LosSerialFtpClientAsm.text includes it).
#
# The steps (before each program is compiled, $LISA_PASCAL/pascal/audit_8char.py
# checks it, with the files it includes, for Lisa Pascal's 8-character
# identifier rule, also against the units it uses):
#   Workshop client (the same steps as LisaSerialFtpClientMake.text):
#   1. assemble LisaSerialFtpClientAsm.text  -> build/LisaSerialFtpClientAsm.OBJ
#   2. compile LisaSerialFtpClient.text      -> build/LisaSerialFtpClientCompiled.OBJ
#   3. link it with LisaSerialFtpClientAsm.OBJ and IOSPASLIB
#                                            -> build/LisaSerialFtpClient.OBJ
#      and put it as LisaSerialFtpClient.obj on the floppy image (users copy it
#      from there into their Workshop) and on the ProFile image
#   Office System client (the same steps as LosSerialFtpClientMake.text):
#   4. icon_48x32.png -> build/LisaSerialFtpClientIcon.BIN ($LISA_PASCAL/icon-tool/png2lisaicon.py)
#   5. assemble LosSerialFtpClientAsm.text   -> build/LosSerialFtpClientAsm.OBJ
#   6. compile LosSerialFtpClient.text       -> build/LosSerialFtpClientCompiled.OBJ
#   7. link with LosSerialFtpClientAsm.OBJ, IOSPASLIB, SYS1LIB, SYS2LIB
#                                            -> build/LosSerialFtpClient.OBJ
#   8. compile LosSerialFtpClientAlerts.text -> {T197001}PHRASE
#   9. put {T197001}Obj / PHRASE / ICON on the disk image and run InstallTool
# The disk images: $DC42_IMAGE, default ../../LisaSerialFtpClient.dc42 (the
# floppy image users get both programs from), and $PROFILE_IMAGE, default
# ../../profile-5MB-LOSAndWorkshop3-UseWith88iorom.image (a ProFile image with
# the Office System, Workshop 3.0 and the Workshop client).
#
#   ./make.sh                 build both and put them on the disk images
#   ./make.sh clean           delete build/
#
# Environment:
#   LISA_PASCAL   the LisaPascalCrossCompiler folder (default: next to this
#                 repository, ../../../LisaPascalCrossCompiler)
#   LISA_BOOT     the boot volume folder (default: $LISA_PASCAL/boot-workshop3.0)
#   DC42_IMAGE    the floppy image to put both programs on
#   PROFILE_IMAGE the ProFile image to put the Workshop client on
#   BUILD_DIR     where the outputs go (default: build)
#   LPC_OPTS      more options for pascal/lpc.py, e.g. --2.0 (the 2.0 build)
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
LISA_PASCAL=${LISA_PASCAL:-$HERE/../../../LisaPascalCrossCompiler}
DC42_IMAGE=${DC42_IMAGE:-$HERE/../../LisaSerialFtpClient.dc42}
PROFILE_IMAGE=${PROFILE_IMAGE:-$HERE/../../profile-5MB-LOSAndWorkshop3-UseWith88iorom.image}
BUILD_DIR=${BUILD_DIR:-$HERE/build}

die() { echo "make.sh: $*" >&2; exit 1; }

cd "$HERE"
case "$1" in
  clean) rm -rf "$BUILD_DIR"; echo "deleted $BUILD_DIR/"; exit 0 ;;
  "")    ;;
  *)     die "unknown argument '$1' (use: clean, or nothing to build)" ;;
esac

[ -d "$LISA_PASCAL" ] || die "no compiler at $LISA_PASCAL (set LISA_PASCAL)"
LISA_PASCAL=$(cd "$LISA_PASCAL" && pwd)
[ -f "$LISA_PASCAL/pascal/lpc.py" ] || die "no pascal/lpc.py in $LISA_PASCAL (set LISA_PASCAL)"
[ -f "$LISA_PASCAL/pascal/audit_8char.py" ] || die "no pascal/audit_8char.py in $LISA_PASCAL"
command -v python3 > /dev/null || die "python3 is needed"
export LISA_BOOT=${LISA_BOOT:-$LISA_PASCAL/boot-workshop3.0}
FS_TOOLS=$LISA_PASCAL/file-system-tools       # LisaFileSystemTool.py, LisaFileSystemToolPerFile.py
ICON_TOOL=$LISA_PASCAL/icon-tool/png2lisaicon.py
[ -f "$FS_TOOLS/LisaFileSystemToolPerFile.py" ] || die "no LisaFileSystemToolPerFile.py in $FS_TOOLS"
[ -f "$DC42_IMAGE" ] || die "no disk image at $DC42_IMAGE (set DC42_IMAGE)"
DC42_IMAGE=$(cd "$(dirname "$DC42_IMAGE")" && pwd)/$(basename "$DC42_IMAGE")
[ -f "$PROFILE_IMAGE" ] || die "no disk image at $PROFILE_IMAGE (set PROFILE_IMAGE)"
PROFILE_IMAGE=$(cd "$(dirname "$PROFILE_IMAGE")" && pwd)/$(basename "$PROFILE_IMAGE")

# The sources go into the build folder, as on one Lisa volume: the programs
# include the shared files by name.
mkdir -p "$BUILD_DIR"
BUILD_DIR=$(cd "$BUILD_DIR" && pwd)
cp "$HERE"/*.text "$BUILD_DIR/"

# audit <program>: the 8-character identifier rule (the program with the files
# it includes, and the units it uses, found through the boot volume's INTRINSIC.LIB)
audit() {
  echo "checking 8-character identifier rules in $1.text (audit_8char.py)"
  ( cd "$BUILD_DIR" && python3 "$LISA_PASCAL/pascal/audit_8char.py" -q --boot "$LISA_BOOT" "$1" ) ||
    die "audit_8char.py found identifier collisions in $1.text"
}

# compile <program> <output>: front end and code generator, with checks
compile() {
  echo "compiling $1.text"
  log="$BUILD_DIR/compile-$1.log"
  ( cd "$BUILD_DIR" && rm -f "$2.OBJ" PROGCOMM.TEXT ./*.I &&
    python3 "$LISA_PASCAL/pascal/lpc.py" $LPC_OPTS "$1" '' "$2" < /dev/null > "$log" 2>&1 &&
    [ -f "$2.OBJ" ] ) ||
    { tail -40 "$log"; die "the compilation failed (see $log)"; }
  echo "  $(grep 'Compilation complete' "$log" | sed 's/^ *//')"
  echo "  $(grep 'Total code size' "$log" | sed 's/^ *//')"
}

# assemble <source> <object>
assemble() {
  echo "assembling $1.text"
  ( cd "$BUILD_DIR" && rm -f "$2.OBJ" &&
    python3 "$LISA_PASCAL/assembler/assembler.py" "$1" '' "$2" > "asm-$1.log" 2>&1 ) ||
    { cat "$BUILD_DIR/asm-$1.log"; die "the assembler failed (see $BUILD_DIR/asm-$1.log)"; }
}

# link <output> <inputs...>
link() {
  out=$1; shift
  echo "linking $out with $*"
  ( cd "$BUILD_DIR" && rm -f "$out.OBJ" &&
    python3 "$LISA_PASCAL/linker/lisalinker.py" -o "$out" -m "$out.map" "$@" > "link-$out.log" 2>&1 ) ||
    { cat "$BUILD_DIR/link-$out.log"; die "the link failed (see $BUILD_DIR/link-$out.log)"; }
  echo "  $(sed -n 's/^\([0-9]* jump table.*\)/\1/p' "$BUILD_DIR/link-$out.log")"
  echo "  $BUILD_DIR/$out.OBJ: $(wc -c < "$BUILD_DIR/$out.OBJ") bytes"
}

echo "=== LisaSerialFtpClient (Lisa Workshop)"
audit LisaSerialFtpClient
assemble LisaSerialFtpClientAsm LisaSerialFtpClientAsm
compile LisaSerialFtpClient LisaSerialFtpClientCompiled
link LisaSerialFtpClient LisaSerialFtpClientCompiled LisaSerialFtpClientAsm IOSPASLIB
for img in "$DC42_IMAGE" "$PROFILE_IMAGE"; do
  echo "putting LisaSerialFtpClient.obj onto $img"
  python3 "$FS_TOOLS/LisaFileSystemToolPerFile.py" put "$img" \
    "$BUILD_DIR/LisaSerialFtpClient.OBJ" 'LisaSerialFtpClient.obj' > "$BUILD_DIR/put.log" 2>&1 ||
    { cat "$BUILD_DIR/put.log"; die "cannot put LisaSerialFtpClient.obj on $img (see $BUILD_DIR/put.log)"; }
done

echo ""
echo "=== LosSerialFtpClient (Lisa Office System)"
[ -f "$ICON_TOOL" ] || die "no $ICON_TOOL"
if [ -f "$HERE/icon_48x32.png" ]; then
  echo "converting the png icon to a Lisa icon"
  python3 "$ICON_TOOL" "$HERE/icon_48x32.png" "$BUILD_DIR/LisaSerialFtpClientIcon.BIN"
fi
audit LosSerialFtpClient
assemble LosSerialFtpClientAsm LosSerialFtpClientAsm
compile LosSerialFtpClient LosSerialFtpClientCompiled
link LosSerialFtpClient LosSerialFtpClientCompiled LosSerialFtpClientAsm IOSPASLIB SYS1LIB SYS2LIB

echo "compiling LosSerialFtpClientAlerts.text"
( cd "$BUILD_DIR" && rm -f '{T197001}PHRASE' &&
  python3 "$LISA_PASCAL/alert/alert.py" "$HERE/LosSerialFtpClientAlerts.text" '{T197001}' > alert.log 2>&1 ) ||
  { cat "$BUILD_DIR/alert.log"; die "alert failed (see $BUILD_DIR/alert.log)"; }
echo "  $(grep 'All .* alerts read' "$BUILD_DIR/alert.log")  $(grep 'All .* menus read' "$BUILD_DIR/alert.log")"
echo "  $BUILD_DIR/{T197001}PHRASE: $(wc -c < "$BUILD_DIR/{T197001}PHRASE") bytes"

for img in "$PROFILE_IMAGE" "$DC42_IMAGE"; do
  echo "installing {T197001} onto $img"
  {
    python3 "$FS_TOOLS/LisaFileSystemToolPerFile.py" put "$img" "$BUILD_DIR/LosSerialFtpClient.OBJ" '{T197001}Obj'
    python3 "$FS_TOOLS/LisaFileSystemToolPerFile.py" put "$img" "$BUILD_DIR/{T197001}PHRASE" '{T197001}PHRASE'
    python3 "$FS_TOOLS/LisaFileSystemToolPerFile.py" put "$img" "$BUILD_DIR/LisaSerialFtpClientIcon.BIN" '{T197001}ICON'
    python3 "$LISA_PASCAL/installtool/installtool.py" --fs-tool "$FS_TOOLS" \
      --image "$img" Y '' 197001 N N 'Serial FTP Client'
  } > "$BUILD_DIR/installtool.log" 2>&1 || { cat "$BUILD_DIR/installtool.log"; die "installtool failed on $img (see $BUILD_DIR/installtool.log)"; }
  echo "  $(grep 'successfully installed' "$BUILD_DIR/installtool.log")"
done

