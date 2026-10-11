# Contributing

How we branch, commit and merge. Short version: branch off `main`, one topic per branch,
Conventional Commit messages, merge through a pull request.

## Branches

`main` always builds and runs (`make build`, `make collect`). Nobody commits to it directly:
changes come in through pull requests.

| Branch | Use | Example |
|---|---|---|
| `feat/<topic>` | New feature | `feat/recorder`, `feat/pi-orbbec` |
| `fix/<topic>` | Bug fix | `fix/joystick-priority` |
| `exp/<topic>` | Experiment, may never merge | `exp/navila-squash-view` |
| `docs/<topic>` | Documentation only | `docs/collection-protocol` |
| `data/<topic>` | Data pipeline / dataset work | `data/aruco-labels` |

- Lowercase, words joined with `-`, short, one topic per branch.
- Start from an up-to-date `main`:
  ```bash
  git switch main && git pull
  git switch -c feat/recorder
  ```
- To build on a specific stage instead, branch from that commit: `git switch -c feat/x <commit>`
  (see `git log --oneline`).
- Keep long-running branches current with `git pull --rebase origin main`.
- Delete the branch once it is merged.

## Commit messages

[Conventional Commits](https://www.conventionalcommits.org/):

```
<type>(<scope>): <what it does, imperative mood, under ~70 chars>

```

| Type | For |
|---|---|
| `feat` | New capability |
| `fix` | Bug fix |
| `build` | Docker images, Makefile, dependencies |
| `docs` | Documentation |
| `refactor` | Code change with no behaviour change |
| `test` | Tests |
| `chore` | Housekeeping |
| `exp` | Experiments |

Scopes: `docker`, `config`, `bringup`, `navila`, `pi`, `recorder`, `stt`, `data`, `training`,
`eval`, `docs`. Leave the scope out if a change spans several.

Examples:

```
feat(recorder): add episode start/stop markers
fix(bringup): remap teleop to cmd_vel_joy so the joystick overrides NaVILA
build(pi): add arm64 Humble image with Orbbec driver
exp(navila): try squash image mode on Orbbec color stream
docs: add collection protocol
```

- One logical change per commit. Write it as a command: "add", not "added".
- If a commit needs "and" to describe it, it is probably two commits.

## Pull requests

- Title in the same format as a commit message.
- Say what changed, how you tested it (on Roxy, on the fake robot, or build only), and anything
  the reviewer should try.
- At least one teammate reviews before merging into `main`.

## Repo rules

- **Never commit** `.env`, model weights, rosbags, datasets or checkpoints. `.gitignore` covers
  them, but check `git status` before committing. Data lives outside git (`data/README.md`).
- **Every new ROS topic** goes in `configs/topics.yaml` first.
- **Every new workflow** gets a `make` target and a line in the README.
- **Robot parameters** live in `configs/robots/` (mounted into the containers, no rebuild needed).
- **Pi-side code** lives in `robot/pi/` and `docker/pi/`.
- **Forks** (go2_ros2_sdk, NaVILA) are not edited inside this repo. Change them in their own repos;
  they will be pulled in as submodules.
