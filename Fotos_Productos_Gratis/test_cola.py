"""Pruebas sin descargar IA: máscara sintética, disco temporal, sin Drive real."""
import os,tempfile,unittest,threading,time
from pathlib import Path
from PIL import Image
os.environ.setdefault('LOCALAPPDATA',tempfile.gettempdir())
from procesador import Worker,init_db,process_photo,MARKER

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        (self.root/MARKER).write_text('ok')
        self.photo=self.root/'Estribos'/'EUNDN01'/'Originales'/'foto.png'
        self.photo.parent.mkdir(parents=True)
        Image.new('RGB',(32,24),(10,20,30)).save(self.photo)
        self.original=self.photo.read_bytes();self.calls=0
        self.db=init_db(self.root/'state.sqlite')
    def tearDown(self):self.db.close();self.temp.cleanup()
    def mask(self,img):
        self.calls+=1
        result=img.convert('RGBA');alpha=Image.new('L',img.size,0)
        alpha.paste(255,(8,6,24,18));result.putalpha(alpha);return result
    def worker(self,fn=None,png=False):
        w=Worker(self.root,png,lambda msg:None,threading.Event(),remove_fn=fn or self.mask)
        st=self.photo.stat();w.observed[str(self.photo.resolve())]=(f'{st.st_size}:{st.st_mtime_ns}',time.monotonic()-20)
        return w
    def test_white_original_and_dimensions(self):
        output,_=process_photo(self.photo,self.root,self.mask,True)
        with Image.open(output) as result:
            self.assertEqual(result.size,(32,24));self.assertTrue(all(c>=250 for c in result.getpixel((0,0))))
        self.assertEqual(self.photo.read_bytes(),self.original)
        self.assertEqual(len(list(self.root.rglob('Sin fondo - Revisar/*.png'))),1)
    def test_restart_skips_finished(self):
        self.worker().scan(self.db);self.worker().scan(self.db)
        self.assertEqual(self.calls,1)
    def test_later_enable_png(self):
        self.worker().scan(self.db);self.worker(png=True).scan(self.db)
        self.assertEqual(self.calls,2)
        self.assertEqual(len(list(self.root.rglob('Sin fondo - Revisar/*.png'))),1)
    def test_partial_write_recovered(self):
        process_photo(self.photo,self.root,self.mask)
        self.worker().scan(self.db)
        self.assertEqual(len(list(self.root.rglob('Fondo blanco - Revisar/*.jpg'))),1)
    def test_errors_survive_restart_and_retry(self):
        def fail(img):raise ValueError('fallo simulado')
        self.worker(fail).scan(self.db)
        self.assertEqual(self.db.execute('SELECT status,attempts FROM jobs').fetchone(),('error',1))
        self.worker().scan(self.db);self.assertEqual(self.calls,0)
        self.db.execute('UPDATE jobs SET retry=0');self.db.commit()
        self.worker().scan(self.db);self.assertEqual(self.calls,1)
    def test_changed_source_processed_again(self):
        self.worker().scan(self.db)
        Image.new('RGB',(33,25),'red').save(self.photo)
        self.worker().scan(self.db);self.assertEqual(self.calls,2)
        self.assertEqual(len(list(self.root.rglob('Fondo blanco - Revisar/*.jpg'))),2)
    def test_new_file_waits(self):
        w=self.worker();w.observed.clear();w.scan(self.db);self.assertEqual(self.calls,0)
    def test_empty_mask_errors_without_output(self):
        with self.assertRaises(ValueError):process_photo(self.photo,self.root,lambda img:Image.new('RGBA',img.size,(0,0,0,0)))
        self.assertFalse(list(self.root.rglob('*.jpg')))

if __name__=='__main__':unittest.main()
