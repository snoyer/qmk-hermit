# QMK hermit

A tool to compile out-of-tree QMK keyboard and layouts.


The script creates a temporary QMK installation by symlinking directory structures 
from both an original QMK source tree and independent keyboard and/or layout sources directories.
The regular QMK build operations are then carried out in that temporary location, 
leaving both the QMK source tree and other sources unmodified.


