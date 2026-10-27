# Native helper

neoscrypt.c and neoscrypt.h: upstream NeoScrypt, commit
89c04c75a674974936c6579b42764d5370139d3d. BSD notices remain in the
source files and ../licenses/NeoScrypt.txt.

harness_plen.c parameterizes the reference password length. The release
uses ../bin/harness_plen.exe with profile 0x80000720 and length 256.

Rebuild from this directory with MSYS2 UCRT64 GCC:

gcc -O2 -Wall -o ../bin/harness_plen.exe harness_plen.c

The shipped binary requires only Windows kernel/UCRT libraries; MSYS2
is not required to run it. Self-check uses synthetic data.
