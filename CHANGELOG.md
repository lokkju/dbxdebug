# Changelog

## [0.5.0](https://github.com/lokkju/dbxdebug/compare/v0.4.0...v0.5.0) (2026-09-06)


### Features

* **api:** derive the package root's exports from the modules' __all__ ([f4fbb2d](https://github.com/lokkju/dbxdebug/commit/f4fbb2d18a7a4b7c329c7593a26e39e99930b1c6)), closes [#7](https://github.com/lokkju/dbxdebug/issues/7)
* **qmp:** add mouse input to QMPClient ([a25a356](https://github.com/lokkju/dbxdebug/commit/a25a356c19115d5ea12189aca3c221413602dc9f)), closes [#2](https://github.com/lokkju/dbxdebug/issues/2)


### Bug Fixes

* **frames:** end steps_out at the return address, and wrap segment reads ([40e2817](https://github.com/lokkju/dbxdebug/commit/40e28174473ada9997f650b84051cf296f72d0dd)), closes [#6](https://github.com/lokkju/dbxdebug/issues/6)
* **gdb:** make the pending-stop queue service itself ([908eae0](https://github.com/lokkju/dbxdebug/commit/908eae0ea621ea460c4627a4f7d4320eaaace81a)), closes [#18](https://github.com/lokkju/dbxdebug/issues/18)

## [0.4.0](https://github.com/lokkju/dbxdebug/compare/v0.3.0...v0.4.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* **session:** DosboxSession now runs the emulator headless by default. A caller that relied on seeing the window -- to watch the guest while debugging, or to drive it by hand -- must now pass headless=False explicitly. Everything else is unaffected: the debug surface is identical headless, and screen capture was verified byte-identical between the two modes. Note also that `headless` sits mid-dataclass, so positional construction past `connect=` shifts.

### Features

* **session:** add DosboxSession(headless=True), on by default ([8af1f56](https://github.com/lokkju/dbxdebug/commit/8af1f56528977c2dbc4a2ddec442f4aa6f76933d)), closes [#3](https://github.com/lokkju/dbxdebug/issues/3)
* **session:** add read_bulk, a one-call bulk memory read ([f8e228b](https://github.com/lokkju/dbxdebug/commit/f8e228b101b14e682e3b4ec98b434f1fd7e5e7c7)), closes [#9](https://github.com/lokkju/dbxdebug/issues/9)


### Bug Fixes

* **ci:** unblock publishing and add a recovery path for a failed one ([9c69a87](https://github.com/lokkju/dbxdebug/commit/9c69a87c73f30b0a249e1bd5ec3c4cc4f2af0736))
* **gdb:** bound reads and resynchronise the packet stream ([5940242](https://github.com/lokkju/dbxdebug/commit/5940242035181668c4d35e61db56d5b2342aa620))
* **session:** record that headless is now the default ([d649d70](https://github.com/lokkju/dbxdebug/commit/d649d705bb46261d38c30a23fc9c39bd390f7c87))
* **video,cli:** let a GDB client be borrowed instead of reopened ([bb9d351](https://github.com/lokkju/dbxdebug/commit/bb9d35170283407f9c7fbd64f4cfd4f1d75f1d64))


### Performance Improvements

* **clients:** set TCP_NODELAY, and correct what read_bulk is now worth ([c535e3b](https://github.com/lokkju/dbxdebug/commit/c535e3bafe2799e708714e4f771046fd35a55d81))


### Documentation

* correct the hazard write-ups now that the GDB stream is fixed ([a1821aa](https://github.com/lokkju/dbxdebug/commit/a1821aa8904987afd73ad1ba97d022fee5e30213)), closes [#4](https://github.com/lokkju/dbxdebug/issues/4) [#5](https://github.com/lokkju/dbxdebug/issues/5)
* **migration:** fix the gaps a real migration hit ([96ffaa7](https://github.com/lokkju/dbxdebug/commit/96ffaa7a901a35529e14a81828f8a36d550664d9))

## [0.3.0](https://github.com/lokkju/dbxdebug/compare/v0.2.1...v0.3.0) (2026-09-05)


### ⚠ BREAKING CHANGES

* **session:** set_breakpoint(seg, off) now computes a LINEAR address via addressing.linear(seg, off) instead of packing seg:off as a far pointer. Current DOSBox-X builds decode Z0/z0 as linear (this package's GDBClient requires dosbox-x-linear-bp+ at connect), so the old packed form would silently misplace the breakpoint while gdbserver still answered OK.
* **gdb:** read_registers()["eip"] is now documented as an offset within CS, not a linear address, matching the fixed GDB stub semantics (register 8 is EIP-within-CS in both g and G directions on stubs that advertise dosbox-x-eip-offset+). Code that combined the old eip value directly as a linear address is silently wrong against a fixed stub; call linear_pc() to get the linear program counter instead.
* **utils:** parse_x86_address (and anything that calls it, including the `dbxdebug mem`/`cpu` CLI commands) now interprets a bare digit string with no "0x" prefix as hexadecimal instead of decimal. Callers that were relying on decimal parsing of unprefixed strings (e.g. passing "1000" and expecting 1000 rather than 4096) must add an explicit base-10 conversion before calling, or use a "0x"-prefixed or already-hex string.
* **gdb:** GDBClient now refuses to connect to a stub that does not advertise dosbox-x-linear-bp+. Such builds split the Z0 argument as a packed far pointer, so any breakpoint above 64 KB answers OK and never fires -- silently. Pass require_capabilities=False to proceed against an old build deliberately.

### Features

* **addressing:** add the linear addressing module ([e73ce82](https://github.com/lokkju/dbxdebug/commit/e73ce82479a8b1883305fb4db2d33b4f0d960e7c))
* **cli:** add session list, session reap and doctor ([8179f94](https://github.com/lokkju/dbxdebug/commit/8179f94e11aa07a536ab027dec7dbc3dd425f16c))
* **frames:** add real-mode stack frame walking ([02c2990](https://github.com/lokkju/dbxdebug/commit/02c29909e1a492e1392d0ddecec994da8e71ea83))
* **gdb:** add register list, linear PC, and register write to GDBClient ([fc5729e](https://github.com/lokkju/dbxdebug/commit/fc5729e6e0561310c49d1ac94a84368bb051deed))
* **gdb:** require dosbox-x-linear-bp+ at connect ([b8cc28f](https://github.com/lokkju/dbxdebug/commit/b8cc28fb31d444a961d8850987eef2bbe0bc6d34))
* **qmp:** wrap memdump, screendump, savestate, loadstate, system_reset and quit ([c7a97c6](https://github.com/lokkju/dbxdebug/commit/c7a97c67561f2b732cff7d62b57f994fe8d922c8))
* **registry:** add the session registry with list and reap ([2af648d](https://github.com/lokkju/dbxdebug/commit/2af648db88c446f1563ed83ab297b22b929f278d))
* **session:** port the DosboxSession lifecycle from a downstream consumer ([70f26d9](https://github.com/lokkju/dbxdebug/commit/70f26d948cc49f57463c9d91657a7c0f896d0372))
* **skills:** ship the two Agent Skills from this repo ([ff01e0d](https://github.com/lokkju/dbxdebug/commit/ff01e0daad568a9de0e031b5f5ed87d56fe1638b))


### Bug Fixes

* add shifted punctuation mappings to char_to_qcode ([6a19fb4](https://github.com/lokkju/dbxdebug/commit/6a19fb47a0de76841815664eeee78b3537502f86))
* **addressing:** validate seg:off components and clarify parse errors ([c5d67a5](https://github.com/lokkju/dbxdebug/commit/c5d67a5792392dd373860f8cf880863d20b8d2e0))
* **cli:** report the real package version instead of a hardcoded 0.1.0 ([8100d53](https://github.com/lokkju/dbxdebug/commit/8100d53cf9c228d31d0ed3997561680c4aa18f04))
* **frames:** correct steps_out threshold and close review gaps ([880a965](https://github.com/lokkju/dbxdebug/commit/880a965c6855a22ae6de2edbc78091a8be05e028))
* **paths:** unify dosbox-x binary resolution between session and doctor ([0bda742](https://github.com/lokkju/dbxdebug/commit/0bda742a35e8f56f74367268612b7fd45e3c528d))
* satisfy ruff C420 and formatting gates ([d464033](https://github.com/lokkju/dbxdebug/commit/d46403357cce148df2d424ac1df54c24fcd296c3))
* **session:** close start()/stop() review gaps from Task 8 round 1 ([6032fcb](https://github.com/lokkju/dbxdebug/commit/6032fcb7f1414149adc8f5363c433bded29f7e86))
* **session:** restore process-group sweep lost by round-1's kill_group pid= ([c3cd6ff](https://github.com/lokkju/dbxdebug/commit/c3cd6ff5faed880ac9d9ef94ec3db2852ce3ab73))
* **tests:** re-scope the desync guard and close integration review gaps ([0832b6d](https://github.com/lokkju/dbxdebug/commit/0832b6d3a6026c89d03c9574b43b5c727b019b88))
* **utils:** parse bare address digits as hex, not decimal ([b0ba5be](https://github.com/lokkju/dbxdebug/commit/b0ba5bedb5b109f4279ba2e4119ee3a4268d0fda))


### Documentation

* add CI, PyPI, and license badges to README ([cb3d2fd](https://github.com/lokkju/dbxdebug/commit/cb3d2fda3cd81c736f20dba706ef90e8e6f3e4b4))
* document the migration path onto dbxdebug ([e868090](https://github.com/lokkju/dbxdebug/commit/e8680907b976fd8ddb3ce7c5cdf5db0301fc6d54))
* **frames:** neutralise downstream references and drop a false claim ([cbbec5a](https://github.com/lokkju/dbxdebug/commit/cbbec5ad2b446ea50902872ad3d76a4034e604ad))
* rewrite the README around the session lifecycle ([5cd96ce](https://github.com/lokkju/dbxdebug/commit/5cd96ce8858f8ca4244237f158f8f067d3ab5f8b))
