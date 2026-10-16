import sys
p = sys.argv[1]
src = open(p, encoding='utf-8').read()
compile(src, p, 'exec')
print('SYNTAX OK', len(src), 'chars')
