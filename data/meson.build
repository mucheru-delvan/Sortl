datadir = get_option('datadir')

desktop_file = files(app_id + '.desktop')
metainfo_file = files(app_id + '.metainfo.xml')

install_data(desktop_file, install_dir: datadir / 'applications')
install_data(metainfo_file, install_dir: datadir / 'metainfo')
install_data(
  'icons/hicolor/scalable/apps' / (app_id + '.svg'),
  install_dir: datadir / 'icons/hicolor/scalable/apps',
)
install_data(
  'icons/hicolor/symbolic/apps' / (app_id + '-symbolic.svg'),
  install_dir: datadir / 'icons/hicolor/symbolic/apps',
)

desktop_validate = find_program('desktop-file-validate', required: false)
if desktop_validate.found()
  test('validate desktop file', desktop_validate, args: [desktop_file])
endif

appstreamcli = find_program('appstreamcli', required: false)
if appstreamcli.found()
  test(
    'validate metainfo',
    appstreamcli,
    args: ['validate', '--no-net', '--explain', metainfo_file],
  )
endif
