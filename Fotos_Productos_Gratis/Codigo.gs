/* Fotos de productos. Copiar también Catalogo.gs e Index.html al proyecto. */
function configurar() {
  const p=PropertiesService.getScriptProperties();
  if(!p.getProperty('ROOT_ID')) {
    const root=DriveApp.createFolder('Fotos de Productos');
    root.createFile('_FOTOS_PRODUCTOS.txt','Carpeta del sistema. No eliminar este archivo.');
    p.setProperty('ROOT_ID',root.getId());
  }
  if(!p.getProperty('CLAVE_EQUIPO')) p.setProperty('CLAVE_EQUIPO',Utilities.getUuid().replace(/-/g,'').slice(0,16));
  console.log('Carpeta: https://drive.google.com/drive/folders/'+p.getProperty('ROOT_ID'));
  console.log('Clave para el equipo: '+p.getProperty('CLAVE_EQUIPO'));
}
function doGet() {
  return HtmlService.createHtmlOutputFromFile('Index').setTitle('Fotos de productos')
    .addMetaTag('viewport','width=device-width, initial-scale=1');
}
function autenticar_(clave) {
  const expected=PropertiesService.getScriptProperties().getProperty('CLAVE_EQUIPO');
  if(!expected || typeof clave!=='string' || clave!==expected) throw new Error('Clave incorrecta.');
}
function obtenerCatalogo(clave) { autenticar_(clave); return CATALOGO; }
function carpeta_(parent,name) {
  const found=parent.getFoldersByName(name);
  return found.hasNext()?found.next():parent.createFolder(name);
}
function safe_(s) { return s.replace(/[<>:"/\\|?*\u0000-\u001f]/g,'_').replace(/[. ]+$/g,'').slice(0,100); }
function guardarFoto(clave,id,b64,mime,uploadId) {
  autenticar_(clave);
  const product=CATALOGO.find(x=>x.id===id);
  if(!product) throw new Error('Producto no válido.');
  const types={'image/jpeg':'jpg','image/png':'png','image/webp':'webp'};
  if(!types[mime] || typeof b64!=='string' || b64.length>28*1024*1024) throw new Error('Usá JPG, PNG o WEBP de hasta 20 MB.');
  if(!/^[a-f0-9-]{36}$/i.test(uploadId)) throw new Error('Identificador no válido.');
  const bytes=Utilities.base64Decode(b64);
  if(bytes.length>20*1024*1024 || bytes.length<12) throw new Error('Tamaño no válido.');
  const u=bytes.slice(0,12).map(x=>(x+256)%256);
  const valid=(mime==='image/jpeg' && u[0]===255 && u[1]===216 && u[2]===255)
    || (mime==='image/png' && u.slice(0,8).join(',')==='137,80,78,71,13,10,26,10')
    || (mime==='image/webp' && String.fromCharCode(...u.slice(0,4))==='RIFF' && String.fromCharCode(...u.slice(8,12))==='WEBP');
  if(!valid) throw new Error('El archivo no corresponde a una imagen admitida.');
  const lock=LockService.getScriptLock(); lock.waitLock(30000);
  try {
    const root=DriveApp.getFolderById(PropertiesService.getScriptProperties().getProperty('ROOT_ID'));
    const cat=carpeta_(root,safe_(product.category));
    const dir=carpeta_(cat,safe_(product.folder));
    const originals=carpeta_(dir,'Originales');
    const name=safe_(product.code)+'_'+uploadId+'.'+types[mime];
    // El mismo intento usa el mismo identificador: reintentar no duplica la foto.
    if(!originals.getFilesByName(name).hasNext()) originals.createFile(Utilities.newBlob(bytes,mime,name));
    if(!dir.getFilesByName('Producto.txt').hasNext()) dir.createFile('Producto.txt',product.code+'\n'+product.description+'\n'+product.section+'\n'+product.category+'\nFuente: '+product.source+', fila '+product.row);
    return {ok:true,code:product.code};
  } finally { lock.releaseLock(); }
}
