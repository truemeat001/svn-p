"""TEST ONLY: real bridge/catalog/label service, synthetic SVN transport.

The fixture reuses the safety test setup to ensure browser integration exercises
real HTTP and one-use plans. No installed SVN, real WC, repository or Shelf.
"""
from test_changelists import ChangelistTests
from server import PrototypeServer

fixture = ChangelistTests('test_preflight_is_read_only_and_binds_full_exact_review')
fixture.setUp()
server = PrototypeServer(0, catalog=fixture.catalog)
print('SYNTHETIC LOCAL CHANGELISTS TEST ONLY ' + server.origin, flush=True)
try:
    server.serve_forever()
finally:
    server.server_close()
    fixture.doCleanups()
