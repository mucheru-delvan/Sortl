project(
  'sortl',
  version: '0.1.1',
  meson_version: '>= 0.64.0',
  license: 'MIT',
)

python = import('python').find_installation('python3', pure: true)
assert(
  python.language_version().version_compare('>= 3.12'),
  'Sortl requires Python 3.12 or newer',
)

app_id = 'io.github.mucheru_delvan.Sortl'

subdir('data')
subdir('src')
subdir('bin')
