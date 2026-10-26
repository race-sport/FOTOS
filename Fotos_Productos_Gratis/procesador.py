"""Procesamiento local v4: nunca elimina originales ni llama a una API de edición.

v4: recorte con BiRefNet (el modelo abierto más preciso para productos; necesita
unos 6-7 GB de RAM libres). Con menos de 12 GB de RAM usa IS-Net automáticamente.
Limpia objetos sueltos, corrige el color de los bordes (sin halo del piso), mejora
contraste y nitidez sin inventar detalles y entrega 2000x2000 con el producto centrado.
"""
import hashlib, io, json, os, queue, sqlite3, tempfile, threading, time, sys
from pathlib import Path

os.environ.setdefault('OMP_NUM_THREADS', str(max(2,(os.cpu_count() or 4)-1)))
BASE=Path(__file__).resolve().parent
STATE=Path(os.environ.get('LOCALAPPDATA',str(Path.home())))/'FotosProductosGratis'
STATE.mkdir(parents=True,exist_ok=True)
CONFIG=STATE/'config.json'
EXTS={'.jpg','.jpeg','.png','.webp'}
MARKER='_FOTOS_PRODUCTOS.txt'
def ram_total_gb():
    try:
        if os.name=='nt':
            import ctypes
            class M(ctypes.Structure):
                _fields_=[('l',ctypes.c_ulong),('ml',ctypes.c_ulong),('t',ctypes.c_ulonglong),('a',ctypes.c_ulonglong),
                          ('tp',ctypes.c_ulonglong),('ap',ctypes.c_ulonglong),('tv',ctypes.c_ulonglong),('av',ctypes.c_ulonglong),('ae',ctypes.c_ulonglong)]
            m=M(); m.l=ctypes.sizeof(M); ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)); return m.t/2**30
        return os.sysconf('SC_PAGE_SIZE')*os.sysconf('SC_PHYS_PAGES')/2**30
    except Exception: return 0
MODELO=os.environ.get('FOTOS_MODELO') or ('birefnet-general' if ram_total_gb()>=12 else 'isnet-general-use')
CARPETA_SALIDA='Fondo blanco'
CARPETA_PNG='Sin fondo'
DBFILE=STATE/('cola_v4_'+MODELO+'.sqlite')   # base nueva: reprocesa todo con el modelo nuevo
LADO=2000      # salida cuadrada en px
MARGEN=0.07    # aire alrededor del producto

def startup_file():
    return Path(os.environ['APPDATA'])/'Microsoft/Windows/Start Menu/Programs/Startup/FotosProductos.vbs'

def set_startup(enabled):
    if os.name!='nt': raise RuntimeError('El inicio automático está disponible en Windows.')
    path=startup_file()
    if not enabled:
        path.unlink(missing_ok=True);return
    pythonw=Path(sys.executable).with_name('pythonw.exe')
    if not pythonw.is_file(): raise RuntimeError('No se encontró pythonw.exe en el entorno del programa.')
    # Codificar correctamente rutas con espacios y caracteres españoles en VBScript.
    command=f'"{pythonw}" "{Path(__file__).resolve()}" --inicio'
    literal='"'+command.replace('"','""')+'"'
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text('Set shell = CreateObject("WScript.Shell")\r\nshell.Run '+literal+', 0, False\r\n',encoding='utf-16')

def atomic_save(image,path,fmt,**kwargs):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,name=tempfile.mkstemp(prefix='.procesando_',suffix='.tmp',dir=path.parent)
    os.close(fd)
    try:
        image.save(name,format=fmt,**kwargs)
        os.replace(name,path)
    finally:
        Path(name).unlink(missing_ok=True)

def limpiar_mascara(m):
    """m: array uint8 0-255. Quita objetos sueltos (carros, cajas) y endurece el borde."""
    import numpy as np
    from scipy import ndimage
    h,w=m.shape
    lab,n=ndimage.label(m>128,structure=np.ones((3,3)))
    if n==0: return m
    areas=np.bincount(lab.ravel())[1:]
    mayor=int(np.argmax(areas))+1
    objs=ndimage.find_objects(lab)
    keep=np.zeros(n+1,bool)
    for i,sl in enumerate(objs,1):
        ys,xs=sl
        toca_borde=ys.start<=1 or xs.start<=1 or ys.stop>=h-1 or xs.stop>=w-1
        keep[i]= i==mayor or (areas[i-1]>=0.04*areas.max() and not toca_borde)
    zona=ndimage.binary_dilation(keep[lab],iterations=4)
    a=m.astype(np.float32)*zona
    a=np.clip((a-35)/(190-35),0,1)
    return (a*255).astype(np.uint8)

def descontaminar(img,alpha):
    """Recalcula el color real del producto en los bordes (saca el gris/amarillo del piso)."""
    import numpy as np
    from PIL import Image, ImageFilter
    from pymatting import estimate_foreground_ml
    w,h=img.size; k=min(1,1600/max(w,h))
    sm=img.resize((round(w*k),round(h*k)),Image.LANCZOS) if k<1 else img
    am=Image.fromarray(alpha).resize(sm.size,Image.LANCZOS)
    fg=estimate_foreground_ml(np.asarray(sm,np.float64)/255,np.asarray(am,np.float64)/255)
    fg=Image.fromarray((np.clip(fg,0,1)*255).astype(np.uint8)).resize(img.size,Image.LANCZOS)
    a=alpha.astype(np.float32)/255
    borde=Image.fromarray((((a>0.02)&(a<0.98))*255).astype(np.uint8)).filter(ImageFilter.MaxFilter(7))
    b=np.asarray(borde,np.float32)[...,None]/255
    out=np.asarray(img,np.float32)*(1-b)+np.asarray(fg,np.float32)*b
    return Image.fromarray(np.clip(out,0,255).astype(np.uint8))

def mejorar(img,alpha):
    """Contraste y nitidez suaves, usando solo lo que hay en la foto (no cambia colores)."""
    import numpy as np
    from PIL import Image, ImageFilter
    arr=np.asarray(img).astype(np.float32)
    sel=alpha>220
    if sel.sum()>500:
        lum=arr[sel]@np.array([0.299,0.587,0.114],np.float32)
        lo=min(np.percentile(lum,0.2),12); hi=max(np.percentile(lum,99.9),230)
        arr=(arr-lo)*min(255.0/max(hi-lo,1),1.15)
    out=Image.fromarray(np.clip(arr,0,255).astype(np.uint8))
    return out.filter(ImageFilter.UnsharpMask(radius=1.8,percent=45,threshold=3))

def encuadrar(img,alpha,fondo):
    """Producto centrado en un cuadrado LADO x LADO con margen parejo."""
    from PIL import Image
    a=Image.fromarray(alpha)
    bbox=a.point(lambda v:255 if v>20 else 0).getbbox()
    if not bbox: raise ValueError('El recorte quedó vacío. Revisar la foto.')
    prod=img.crop(bbox); am=a.crop(bbox)
    util=int(LADO*(1-2*MARGEN)); k=util/max(prod.size)
    nw,nh=max(1,round(prod.width*k)),max(1,round(prod.height*k))
    prod=prod.resize((nw,nh),Image.LANCZOS); am=am.resize((nw,nh),Image.LANCZOS)
    if fondo is None:
        lienzo=Image.new('RGBA',(LADO,LADO),(0,0,0,0)); prod=prod.convert('RGBA'); prod.putalpha(am)
        lienzo.paste(prod,((LADO-nw)//2,(LADO-nh)//2),prod)
    else:
        lienzo=Image.new('RGB',(LADO,LADO),fondo); lienzo.paste(prod,((LADO-nw)//2,(LADO-nh)//2),am)
    return lienzo

def process_photo(source,root,remove_fn,transparent=False):
    """remove_fn(img) devuelve la máscara (imagen L 0-255) del producto."""
    import numpy as np
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS=60_000_000
    before=source.stat()
    data=source.read_bytes()
    after=source.stat()
    if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
        raise RuntimeError('La foto todavía está sincronizando.')
    digest=hashlib.sha256(data).hexdigest()
    with Image.open(io.BytesIO(data)) as raw:
        if raw.width*raw.height>60_000_000: raise ValueError('La imagen supera 60 megapíxeles.')
        img=ImageOps.exif_transpose(raw).convert('RGB')
    m=remove_fn(img)
    mask=m.getchannel('A') if m.mode in ('RGBA','LA') else m.convert('L')
    if mask.size!=img.size: mask=mask.resize(img.size)
    alpha=limpiar_mascara(np.asarray(mask))
    if alpha.max()==0: raise ValueError('El recorte quedó vacío. Revisar la foto.')
    try: img=descontaminar(img,alpha)
    except Exception: pass  # si falla, sigue sin este paso
    mejor=mejorar(img,alpha)
    product=source.parent.parent
    output=product/CARPETA_SALIDA/(source.stem+'.jpg')
    atomic_save(encuadrar(mejor,alpha,'white'),output,'JPEG',quality=95,subsampling=0)
    if transparent: atomic_save(encuadrar(mejor,alpha,None),product/CARPETA_PNG/(source.stem+'.png'),'PNG')
    return output,digest

def init_db(path):
    db=sqlite3.connect(path)
    db.execute('CREATE TABLE IF NOT EXISTS jobs (source TEXT PRIMARY KEY, fingerprint TEXT, status TEXT, output TEXT, attempts INTEGER DEFAULT 0, retry REAL DEFAULT 0, error TEXT)')
    db.commit(); return db

class Worker:
    def __init__(self,root,transparent,notify,stop,dbpath=None,remove_fn=None):
        self.root=Path(root).resolve(); self.transparent=transparent; self.notify=notify; self.stop=stop
        self.dbpath=dbpath or DBFILE; self.remove_fn=remove_fn; self.observed={}
    def run(self):
        db=None
        try:
            if not (self.root/MARKER).is_file(): self.notify('Esperando a que Google Drive esté disponible…')
            while not (self.root/MARKER).is_file():
                if self.stop.wait(15): return
            db=init_db(self.dbpath)
            if self.remove_fn is None:
                self.notify('Cargando IA local ('+MODELO+'). La primera vez descarga el modelo (BiRefNet: unos 970 MB); puede tardar varios minutos.')
                import onnxruntime as ort
                from rembg import new_session,remove
                opts=ort.SessionOptions(); opts.intra_op_num_threads=max(2,(os.cpu_count() or 4)-1); opts.inter_op_num_threads=1
                session=new_session(MODELO,sess_opts=opts,providers=['CPUExecutionProvider'])
                self.remove_fn=lambda image:remove(image,session=session,only_mask=True)
            self.notify('Listo. Esperando fotos en Originales. Podés minimizar esta ventana.')
            drive_missing=False
            while not self.stop.is_set():
                if not (self.root/MARKER).is_file():
                    if not drive_missing:self.notify('Drive no está disponible. Reintento automático en 15 segundos.')
                    drive_missing=True
                else:
                    if drive_missing:self.notify('Drive disponible. Retomando pendientes.');drive_missing=False
                    try:self.scan(db)
                    except (OSError,RuntimeError) as e:self.notify('Esperando Drive: '+str(e))
                self.stop.wait(15)
        except Exception as e: self.notify('No se pudo continuar: '+str(e))
        finally:
            if db: db.close()
            self.notify('Procesamiento detenido. Las pendientes se conservan.')
    def scan(self,db):
        if not (self.root/MARKER).exists(): raise RuntimeError('Drive no está disponible. Abrilo y volvé a iniciar.')
        for source in self.root.glob('*/*/Originales/*'):
            if self.stop.is_set(): break
            if source.suffix.lower() not in EXTS or not source.is_file(): continue
            key=str(source.resolve())
            try:
                st=source.stat(); fp=f'{st.st_size}:{st.st_mtime_ns}'
                old=self.observed.get(key)
                if not old or old[0]!=fp: self.observed[key]=(fp,time.monotonic()); continue
                if time.monotonic()-old[1]<10: continue
                row=db.execute('SELECT fingerprint,status,output,attempts,retry FROM jobs WHERE source=?',(key,)).fetchone()
                if row and row[0]==fp:
                    if row[1]=='done' and Path(row[2]).is_file():
                        png_path=source.parent.parent/CARPETA_PNG/(Path(row[2]).stem+'.png')
                        if not self.transparent or png_path.is_file(): continue
                    if row[3]>=3 or row[4]>time.time(): continue
                    attempts=row[3]
                else: attempts=0
                self.notify('Procesando: '+source.name)
                try:
                    output,_=process_photo(source,self.root,self.remove_fn,self.transparent)
                    db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',(key,fp,'done',str(output),0,0,''));db.commit()
                    self.notify('✓ Fondo blanco listo: '+source.name)
                except Exception as e:
                    attempts+=1
                    db.execute('INSERT OR REPLACE INTO jobs VALUES (?,?,?,?,?,?,?)',(key,fp,'error','',attempts,time.time()+60*attempts,str(e)));db.commit()
                    self.notify('Error '+str(attempts)+'/3: '+source.name+' — '+str(e))
            except OSError as e: self.notify('Esperando sincronización: '+source.name+' — '+str(e))

def main():
    import tkinter as tk
    from tkinter import ttk,filedialog,messagebox
    import socket
    guard=socket.socket()
    try: guard.bind(('127.0.0.1',47863))
    except OSError:
        if '--inicio' in sys.argv:return
        r=tk.Tk();r.withdraw();messagebox.showinfo('Fotos de productos','El programa ya está abierto. Buscalo en la barra de tareas.');return
    root=tk.Tk();root.title('Fotos de productos · IA local v4 · '+MODELO);root.geometry('850x650');root.minsize(650,520)
    cfg={}
    try: cfg=json.loads(CONFIG.read_text(encoding='utf-8'))
    except (OSError,ValueError): pass
    path=tk.StringVar(value=cfg.get('folder','')); png=tk.BooleanVar(value=cfg.get('transparent',False))
    messages=queue.Queue();stop=threading.Event();thread=None
    frame=ttk.Frame(root,padding=22);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Fotos de productos',font=('Segoe UI',22,'bold')).pack(anchor='w')
    ttk.Label(frame,text='Procesamiento local · Una foto por vez · Sin créditos por imagen').pack(anchor='w',pady=(5,20))
    ttk.Label(frame,text='Carpeta sincronizada de Drive: Fotos de Productos').pack(anchor='w')
    ttk.Entry(frame,textvariable=path).pack(fill='x',pady=8)
    def browse():
        folder=filedialog.askdirectory(title='Elegí Fotos de Productos dentro de Google Drive')
        if folder:path.set(folder)
    choose=ttk.Button(frame,text='Elegir carpeta…',command=browse);choose.pack(anchor='w')
    checkbox=ttk.Checkbutton(frame,text='Guardar también PNG transparente (ocupa más espacio)',variable=png);checkbox.pack(anchor='w',pady=12)
    auto_windows=tk.BooleanVar(value=os.name=='nt' and startup_file().exists())
    def change_startup():
        try:
            set_startup(auto_windows.get())
            messages.put('Inicio con Windows activado. No muevas esta carpeta del programa.' if auto_windows.get() else 'Inicio con Windows desactivado.')
        except Exception as e:
            auto_windows.set(not auto_windows.get());messagebox.showerror('Inicio automático',str(e))
    ttk.Checkbutton(frame,text='Abrir y procesar automáticamente al iniciar sesión en Windows',variable=auto_windows,command=change_startup).pack(anchor='w',pady=(0,8))
    buttons=ttk.Frame(frame);buttons.pack(fill='x',pady=8)
    def start(automatic=False):
        nonlocal thread
        if thread and thread.is_alive():return
        if not path.get():return
        if not automatic and not (Path(path.get())/MARKER).is_file():messagebox.showerror('Carpeta incorrecta','Buscá Fotos de Productos dentro de Google Drive. Debe contener '+MARKER);return
        CONFIG.write_text(json.dumps({'folder':path.get(),'transparent':png.get()},ensure_ascii=False),encoding='utf-8')
        stop.clear();thread=threading.Thread(target=Worker(path.get(),png.get(),messages.put,stop).run,daemon=True);thread.start()
    def pause():stop.set();messages.put('Pausando al terminar la foto actual…')
    ttk.Button(buttons,text='Iniciar / Reanudar',command=start).pack(side='left',padx=(0,8))
    ttk.Button(buttons,text='Pausar',command=pause).pack(side='left',padx=(0,8))
    def retry():
        if thread and thread.is_alive():messagebox.showinfo('Pausá primero','Pausá y esperá a que termine la foto actual.');return
        db=init_db(DBFILE);db.execute("UPDATE jobs SET attempts=0,retry=0 WHERE status='error'");db.commit();db.close();messages.put('Errores habilitados para reintentar. Tocá Iniciar.')
    ttk.Button(buttons,text='Reintentar errores',command=retry).pack(side='left')
    log=tk.Text(frame,height=15,wrap='word',font=('Consolas',10),state='disabled');log.pack(fill='both',expand=True,pady=12)
    ttk.Label(frame,text='Los resultados requieren revisión. Drive debe terminar de sincronizar para verlos en el celular.',wraplength=780).pack(anchor='w')
    def poll():
        while not messages.empty():
            msg=time.strftime('%H:%M:%S')+'  '+messages.get()
            log.configure(state='normal');log.insert('end',msg+'\n');log.see('end');log.configure(state='disabled')
            with (STATE/'registro.log').open('a',encoding='utf-8') as f:f.write(msg+'\n')
        root.after(300,poll)
    def close():
        if thread and thread.is_alive():
            pause();messagebox.showinfo('Procesamiento en curso','Se solicitó pausar. Esperá a que aparezca “Procesamiento detenido” y cerrá de nuevo. Podés minimizar mientras tanto.')
        else:root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);poll()
    if cfg.get('folder'):root.after(400,lambda:start(automatic=True))
    if '--inicio' in sys.argv:root.after(200,root.iconify)
    root.mainloop();guard.close()

if __name__=='__main__':main()
