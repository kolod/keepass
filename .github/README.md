# KeePass 2.x source mirror

Unofficial mirror of the [KeePass Password Safe](https://keepass.info/) 2.x
source code, built from the official `KeePass-<version>-Source.zip` archives
published on [SourceForge](https://sourceforge.net/projects/keepass/files/KeePass%202.x/).

KeePass is developed by Dominik Reichl. This repository only republishes the
released sources; it is not affiliated with the KeePass project. Please report
bugs and send contributions to the [official project](https://keepass.info/).

## Layout

- Every release is one commit, tagged `v<version>` (e.g. `v2.61.1`), in
  version order, starting with `v2.00` (2.00 Alpha, 2007).
- The repository root contains the unmodified contents of the source archive.
- Commit dates are the SourceForge upload dates of the archives.
- Commit messages contain the change list from `Docs/History.txt` and the
  archive name and SHA-256.

## Updating

The [Mirror KeePass sources](workflows/mirror.yml) workflow is run manually
(Actions → Mirror KeePass sources → Run workflow). It finds the latest `v*`
tag, then downloads, verifies and commits every newer release from SourceForge.
The script can also be run locally:

```sh
python .github/scripts/mirror.py --dry-run   # show what would be committed
python .github/scripts/mirror.py             # commit and tag new releases
git push --follow-tags
```

## License

KeePass is distributed under the GNU General Public License v2 or later;
see `Docs/License.txt` in the sources.
