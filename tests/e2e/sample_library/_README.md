# Fictional sample library (profile-building tests)

Every reference, author and identifier here is invented (`Example Author A`,
`10.0000/example.N`). `private/` holds decoys that the privacy gate must never open
(matched by the default `archive.sensitive_path_regex`). File names avoid the substring
`exam` because that default regex has no word boundaries. Regenerate with
`python tests/fixtures/profile/build_fixtures.py`.
