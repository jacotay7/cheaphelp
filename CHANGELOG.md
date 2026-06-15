# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](http://keepachangelog.com/en/1.0.0/)
and this project adheres to [Semantic Versioning](http://semver.org/spec/v2.0.0.html).

<!-- insertion marker -->
## [1.0.0](https://github.com/jacotay7/cheaphelp/releases/tag/1.0.0) - 2026-06-14

<small>[Compare with first commit](https://github.com/jacotay7/cheaphelp/compare/4e6e0365dc8de03ebbfd97a3d418cb181608799d...1.0.0)</small>

### Features

- `--linger` flag to enable lingering during install (#102) ([544c7d5](https://github.com/jacotay7/cheaphelp/commit/544c7d58a6f61bcba8bf9d2fe7fb6a9fe68baa15) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- repair a failing quality gate with a fixer before replanning (#101) ([9210638](https://github.com/jacotay7/cheaphelp/commit/92106383bfae50347649e397dbbf66960c0063a4) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- include per-issue cost breakdown in opened PR descriptions (#100) ([ada7503](https://github.com/jacotay7/cheaphelp/commit/ada7503da108be365431b35c579e97d7b4d9515b) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- surface service run result in `systemd status` (#94) ([cecc993](https://github.com/jacotay7/cheaphelp/commit/cecc9930d8d5fe57ceac855b285d57834c813f39) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- show today's spend vs daily budget footer (#95) ([29d8c5d](https://github.com/jacotay7/cheaphelp/commit/29d8c5df8db95c79b6b98a3855368e1d196eccbe) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- persist unparseable agent output to last_unparsed_<role>.log (#93) ([4ca4aaa](https://github.com/jacotay7/cheaphelp/commit/4ca4aaa89cf12b7298f7ef153bed62c0cbbf6c16) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- report systemd timer/service health in cheaphelp doctor (#92) ([f2958c2](https://github.com/jacotay7/cheaphelp/commit/f2958c248a77372561988b6cd5d3a17368d01cb2) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- add daily spend budget guardrail with warn/exceeded comments (#74) ([728aaf6](https://github.com/jacotay7/cheaphelp/commit/728aaf60f61e27f18719b5a168cfdb7bad0431f5) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Label needs-human on comment; route back to responder on human reply (#73) ([3f3c493](https://github.com/jacotay7/cheaphelp/commit/3f3c49315b2a87f072edd588f986584ac07b5138) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Allow `cheaphelp run` for many ticks (`-n` / `--continuous`) (#75) ([5b52ed9](https://github.com/jacotay7/cheaphelp/commit/5b52ed9586b24c3c257982bf2cc8765ac854c2bc) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Add `cheaphelp retry` to un-stick issues labeled needs-human (#72) ([c1bead1](https://github.com/jacotay7/cheaphelp/commit/c1bead1fcabb762ee818fb1c257230af366eabe4) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Act on PR review feedback instead of leaving issues stuck in-review (#68) ([25f5f3f](https://github.com/jacotay7/cheaphelp/commit/25f5f3fdca0735b00a6c6d95f003660865670d56) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Add `cheaphelp config` subcommand (show/get/set) with auto-regen (#63) ([1d26895](https://github.com/jacotay7/cheaphelp/commit/1d26895af99eab3f8ea4af08b4ae118d140ff0bb) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>, Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- track token usage and cost per tick, per role, and per issue (#64) ([e087550](https://github.com/jacotay7/cheaphelp/commit/e0875507cf49aa6fdca30315183296292e7fe6ec) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>, Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- retry transient GitHub and OpenRouter failures with backoff (#62) ([2daca10](https://github.com/jacotay7/cheaphelp/commit/2daca1020f7ce4156e314d6b0927dd17d7594645) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- add `cheaphelp logs` subcommand for viewing and following run activity (#61) ([f3e99a3](https://github.com/jacotay7/cheaphelp/commit/f3e99a30252320c1122909d1c90ca549947c1a86) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Blast-radius guardrail caps PR diff size (max files/lines) (#60) ([6037d91](https://github.com/jacotay7/cheaphelp/commit/6037d913867c2cb145e9928b97d0e8b98d47957a) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Let agents follow a project's own contribution conventions (#59) ([7a8e8be](https://github.com/jacotay7/cheaphelp/commit/7a8e8bed6f699d7fa755a032bcf061494f481328) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Prune build clones for closed issues; add `cheaphelp clean` (#45) ([302eec9](https://github.com/jacotay7/cheaphelp/commit/302eec9601892308fa0983f22660b8af0d2f7828) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Retry timed-out tasks; re-prompt agents that emit no decision (#44) ([17e8ab6](https://github.com/jacotay7/cheaphelp/commit/17e8ab6b7cf95f4cfccb1ec9d93ba2863fc85a67) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Hold issues until their dependencies close (#35 part 2) (#43) ([4aeb146](https://github.com/jacotay7/cheaphelp/commit/4aeb14659313a4b63ce8c2fa90226980024566e4) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Parallel ticks via per-issue locks (#35 part 1) (#42) ([0ba4a4e](https://github.com/jacotay7/cheaphelp/commit/0ba4a4ecc83cf14da46048247880464c4262fc7d) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Log each agent step as it starts; trim crash logs (#40) ([01efe1b](https://github.com/jacotay7/cheaphelp/commit/01efe1bb735bed7789245365522875dd7f6cbcd2) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Cap worker tasks run per issue per tick (max_tasks_per_tick) (#39) ([5c7b138](https://github.com/jacotay7/cheaphelp/commit/5c7b138c75ee543690ce33d39671202639dad488) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Cap issues processed per tick (--max-issues / max… (#38) ([a34dbfd](https://github.com/jacotay7/cheaphelp/commit/a34dbfd325067bb8ecbf20e72f9447cdfbd48251) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Add cheaphelp status command (#27) ([96e0cb6](https://github.com/jacotay7/cheaphelp/commit/96e0cb645b172b79d090a0327d4e92d7e76ccecc) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- add workspace run-lock to serialise overlapping ticks (#25) ([bfc4778](https://github.com/jacotay7/cheaphelp/commit/bfc477837280eaa3aa82c923cc47dbfe6eff1fe2) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>, Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- make opencode agent subprocess timeout configurable (#21) ([62a4e7a](https://github.com/jacotay7/cheaphelp/commit/62a4e7a81c2eb66c9bc2a7efd16bd6a50368b099) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- add `cheaphelp repo set` to update checks/autofix in place (#19) ([6f66999](https://github.com/jacotay7/cheaphelp/commit/6f669994b642016ebbed484ff37a2ea665a927f7) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Prefix every GitHub message with a cheaphelp attribution header (#17) ([c001847](https://github.com/jacotay7/cheaphelp/commit/c0018477f42d4501ab3f230106da47de049e1671) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Hand the exact quality-gate commands to the worker (#12) ([a5ddec5](https://github.com/jacotay7/cheaphelp/commit/a5ddec5d42f291d998912893a24d5fa915cff2db) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Bug Fixes

- stop finalize replies promising a 'spec below' (#99) ([942db6e](https://github.com/jacotay7/cheaphelp/commit/942db6e1b54a8fa416d98be6198c1e89f8034a08) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- balance-brace JSON extraction and retry unparseable clean exits (#98) ([1232a80](https://github.com/jacotay7/cheaphelp/commit/1232a805d58d6f22d254535f1cc0eca1572b541f) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Drop stale bot_login args from responder test call sites (#69) ([e9be0c0](https://github.com/jacotay7/cheaphelp/commit/e9be0c0f76105340d90af285821b9b5432cee62c) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Identify bot comments by marker only, not by author login (#67) ([e554d7b](https://github.com/jacotay7/cheaphelp/commit/e554d7bf5f43f0727ceb27fcd2705e43073534cc) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Route push failures to needs-human; require workflow PAT scope (#65) ([587b8d5](https://github.com/jacotay7/cheaphelp/commit/587b8d598ae80aeb1dd1e2db3ac3241c727d2ee1) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- Re-validate issue state under its lock so overlapping ticks don't double-act (#58) ([24c279e](https://github.com/jacotay7/cheaphelp/commit/24c279e0ce4888f050b52a792ab34b17cd14aa1e) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
- distinguish `repo set` no-op from real update (#34) ([264cd6c](https://github.com/jacotay7/cheaphelp/commit/264cd6c8243821527ad208a64bb0982b9157e5f7) by Jacob Taylor). Co-authored-by: cheaphelp[bot] <cheaphelp@users.noreply.github.com>
- Keep the spec at requirements altitude, ask only material questions (#14) ([04a9010](https://github.com/jacotay7/cheaphelp/commit/04a9010a347e90c536bf4b8670b73cd2dcc35de5) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>

### Code Refactoring

- Don't run the full quality gate after every task (#29) ([cf0de8e](https://github.com/jacotay7/cheaphelp/commit/cf0de8e5a3c658ee9b9987ffb6c52217ce11b340) by Jacob Taylor). Co-authored-by: Claude Opus 4.8 <noreply@anthropic.com>
