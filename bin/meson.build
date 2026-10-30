launcher_conf = configuration_data()
launcher_conf.set('PYTHON', python.full_path())

configure_file(
  input: 'sortl.in',
  output: 'sortl',
  configuration: launcher_conf,
  install: true,
  install_dir: get_option('bindir'),
  install_mode: 'rwxr-xr-x',
)
