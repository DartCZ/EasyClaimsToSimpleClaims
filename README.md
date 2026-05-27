# EasyClaims to SimpleClaims Migration Tool

`migrate_easyclaims_to_simpleclaims.py` converts stored
[EasyClaims](https://github.com/cryptobench/EasyClaims) claim data into the
SQLite database format used by
[SimpleClaims](https://github.com/Buuz135/SimpleClaims) `1.0.38`
(`aa198e4`).

## What Is Migrated

- Each EasyClaims owner is converted into a dedicated SimpleClaims party with
  their claimed chunks.
- Player names and all EasyClaims trusted-player storage variants are
  supported: `trustedPlayersData`, `trustedPlayersWithNames`, and
  `trustedPlayers`.
- Trusted players are converted into SimpleClaims `player_allies` with
  individual permission overrides, rather than party members. They do not
  gain permission to claim, unclaim, or invite players simply because they
  were trusted in EasyClaims.
- Each player's calculated EasyClaims claim capacity is stored as the base
  claim limit of the migrated SimpleClaims party.
- EasyClaims admin claims are converted into admin-style parties grouped by
  display name and PvP setting, because SimpleClaims stores names and PvP
  behavior at party level.
- `index.json` is used to validate ownership. If a claim exists in the index
  but its detailed player record is missing, the tool can recover that claim
  and emits a warning in the migration report.

## Known Limitations

- EasyClaims trust level `damage` has no exact SimpleClaims equivalent.
  SimpleClaims uses the same permission for damaging and breaking blocks. By
  default, migrated `damage` trust does not grant block breaking. Use
  `--damage-as-break` to choose the less restrictive approximation.
- EasyClaims playtime is not copied into SimpleClaims in-progress playtime
  tracking. The capacity already earned from EasyClaims playtime is included
  in the migrated party limit; copying the elapsed seconds as well could award
  duplicate bonus claims when SimpleClaims starts.
- To preserve migrated per-player limits precisely, keep
  `ScaleClaimLimitByMembers` disabled in SimpleClaims. Future growth rules are
  controlled by the SimpleClaims configuration.
- If many trust relationships are migrated, configure `MaxPartyAllies` in
  SimpleClaims as `-1` or set it high enough for the migrated data.
- EasyClaims buffer zones are behavior/configuration, not stored ownership
  records. Configure the corresponding SimpleClaims feature separately with
  `EnablePerimeterReservation` if needed.

## Requirements

- Python 3.10 or newer
- No third-party Python dependencies; the tool only uses Python's standard
  library, including `sqlite3`

## Usage

Stop the server and back up its universe directory before migrating.

First, inspect the source data without writing a database:

```powershell
python .\migrate_easyclaims_to_simpleclaims.py `
  --easyclaims-data 'C:\path\to\EasyClaims-data' `
  --dry-run
```

Create a new SimpleClaims database:

```powershell
python .\migrate_easyclaims_to_simpleclaims.py `
  --easyclaims-data 'C:\path\to\EasyClaims-data' `
  --output 'C:\path\to\universe\SimpleClaims\SimpleClaims.db' `
  --report '.\migration-report.json'
```

On Linux or macOS:

```bash
python3 ./migrate_easyclaims_to_simpleclaims.py \
  --easyclaims-data '/path/to/EasyClaims-data' \
  --output '/path/to/universe/SimpleClaims/SimpleClaims.db' \
  --report './migration-report.json'
```

`--easyclaims-data` accepts either the EasyClaims plugin data directory
containing a `claims/` subdirectory, or the `claims/` directory itself.

The tool will not replace an existing `SimpleClaims.db` unless `--overwrite`
is supplied. When overwriting, it creates a timestamped backup next to the
existing database first.

### Optional Arguments

| Option | Description |
| --- | --- |
| `--dry-run` | Parse and summarize the migration without creating a database. |
| `--overwrite` | Replace an existing output database after creating a backup. |
| `--report <path>` | Write migration counts and warnings as JSON. |
| `--damage-as-break` | Map EasyClaims `damage` trust to SimpleClaims block-breaking permission. |
| `--no-limit-overrides` | Do not preserve calculated EasyClaims capacities as party base limits. |

## Installation After Migration

After generating the database:

1. Keep the server stopped.
2. Disable or remove EasyClaims.
3. Install SimpleClaims.
4. Place the generated database at:

```text
<universe>/SimpleClaims/SimpleClaims.db
```

Do not leave old SimpleClaims JSON data files in the target directory:

```text
Parties.json
Claims.json
NameCache.json
AdminOverrides.json
```

If those files are present, SimpleClaims may run its own legacy JSON migration
on startup.

## Trust Mapping

| EasyClaims Trust Level | SimpleClaims Migrated Access |
| --- | --- |
| `none` | No ally relationship is created. |
| `use` | Basic interactions such as doors, chairs, and portals. |
| `container` | `use` access plus chests/containers. |
| `workstation` | `container` access plus benches/workstations. |
| `damage` | `workstation` access by default; optionally block breaking with `--damage-as-break`. |
| `build` | Full migrated block placement and block breaking access. |

All migrated trusted players retain the unrestricted entry and tamed-entity
behavior that EasyClaims did not restrict.

## Testing

Run the included test suite with:

```bash
python -m unittest -v test_migrate_easyclaims_to_simpleclaims.py
```

The tests cover claim conversion, calculated limits, admin PvP grouping,
granular trusted-player permissions, index recovery, dry-run behavior, and
the optional `damage` mapping.
