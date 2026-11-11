MIT License

Copyright (c) 2025 heavykenny

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Optional Oxlint runtime

The JavaScript/TypeScript adapter uses separately installed Oxlint1.90.0 (MIT),
https://github.com/oxc-project/oxc/tree/9b6fff42fa6edf0bec55861b6f0764c7b9acd86b.
Copyright (c) 2024-present VoidZero Inc. & Contributors; copyright (c) 2023 Boshen.
The published dependency retains its own LICENSE. No Oxlint implementation or binary
is vendored into this Python package; our adapter consumes its CLI JSON protocol.

## Optional Tree-sitter function frontend

JS/TS function extraction imports the separately installed MIT-licensed packages
`tree-sitter==0.25.2`, `tree-sitter-javascript==0.25.0` and
`tree-sitter-typescript==0.23.2` from the official tree-sitter organization.
Their wheels retain their own LICENSE files. No grammar implementation is copied
into this repository. Dependency pins deliberately exclude the confirmed unsafe
Point getter behavior in py-tree-sitter0.26.0.
