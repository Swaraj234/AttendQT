import os, sys
print('CWD:', os.getcwd())
print('sys.path[0]:', sys.path[0])
try:
    import main
    print('Imported main successfully')
    print('app title:', getattr(main, 'app', None).title if hasattr(main, 'app') else None)
except Exception as e:
    print('Import error:', e)
