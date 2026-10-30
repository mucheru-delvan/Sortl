core_sources = files(
  'sortl/__init__.py',
  'sortl/executor.py',
  'sortl/journal.py',
  'sortl/models.py',
  'sortl/planner.py',
  'sortl/recovery.py',
  'sortl/service.py',
)

ui_sources = files(
  'sortl/ui/__init__.py',
  'sortl/ui/app.py',
  'sortl/ui/threads.py',
  'sortl/ui/window.py',
)

python.install_sources(core_sources, subdir: 'sortl')
python.install_sources(ui_sources, subdir: 'sortl/ui')
