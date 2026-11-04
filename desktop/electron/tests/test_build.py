import hashlib, importlib.util, json, stat, tempfile, unittest, zipfile
from pathlib import Path
spec=importlib.util.spec_from_file_location('electron_builder',Path(__file__).resolve().parents[1]/'build.py'); build=importlib.util.module_from_spec(spec);spec.loader.exec_module(build)
class ArchiveSafetyTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name);self.out=self.root/'out';self.out.mkdir();self.zip=self.root/'input.zip'
 def make(self,entries):
  with zipfile.ZipFile(self.zip,'w') as z:
   for name,data in entries:z.writestr(name,data)
 def test_regular_archive(self):
  self.make([('a/b.txt',b'hello')]);self.assertEqual(build.unzip_checked(self.zip,self.out),{'a/b.txt'});self.assertEqual(build.inventory(self.out),{'a/b.txt':hashlib.sha256(b'hello').hexdigest()})
 def test_prefix(self):
  self.make([('pkg/Bridge/a',b'a'),('pkg/private',b'no')]);build.unzip_checked(self.zip,self.out,'pkg/Bridge/');self.assertEqual(list(build.inventory(self.out)),['a'])
 def test_unsafe_paths(self):
  for name in ['../outside','/absolute','dir/../outside','a\\b','C:stream','a/./b']:
   with self.subTest(name=name):
    self.make([(name,b'x')]);self.assertRaises(ValueError,build.unzip_checked,self.zip,self.out)
 def test_case_collision(self):
  self.make([('a',b'x'),('A',b'y')]);self.assertRaises(ValueError,build.unzip_checked,self.zip,self.out)
 def test_symlink(self):
  item=zipfile.ZipInfo('link');item.create_system=3;item.external_attr=(stat.S_IFLNK|0o777)<<16
  self.make([(item,b'../outside')]);self.assertRaises(ValueError,build.unzip_checked,self.zip,self.out)
 def test_existing_file_not_overwritten(self):
  (self.out/'a').write_bytes(b'keep');self.make([('a',b'new')]);self.assertRaises(FileExistsError,build.unzip_checked,self.zip,self.out);self.assertEqual((self.out/'a').read_bytes(),b'keep')
if __name__=='__main__':unittest.main()
