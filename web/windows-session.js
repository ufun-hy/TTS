// Each page has its own lease, so closing one tab cannot stop another tab.
(()=>{
 const id=crypto.randomUUID();
 const endpoint='/api/session/view/'+id;
 const ping=()=>fetch(endpoint,{method:'POST'}).catch(()=>{});
 ping();setInterval(ping,10000);
 window.addEventListener('pageshow',ping);
 document.addEventListener('visibilitychange',()=>{if(!document.hidden)ping()});
 window.addEventListener('pagehide',()=>navigator.sendBeacon(endpoint+'/close',''));
})();
