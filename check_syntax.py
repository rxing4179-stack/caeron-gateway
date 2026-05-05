import py_compile
py_compile.compile('/home/ubuntu/caeron-gateway/main.py', doraise=True)
print('main.py Syntax OK')
py_compile.compile('/home/ubuntu/caeron-gateway/injection.py', doraise=True)
print('injection.py Syntax OK')
py_compile.compile('/home/ubuntu/caeron-gateway/summarizer.py', doraise=True)
print('summarizer.py Syntax OK')

