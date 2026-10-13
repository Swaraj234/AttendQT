import main
print('Imported main, app title:', getattr(main, 'app', None).title if hasattr(main, 'app') else 'no app')
