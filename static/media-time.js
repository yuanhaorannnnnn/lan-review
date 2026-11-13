(function(root){
  function formatTime(seconds, alwaysHours=false){
    const ms=Math.round(seconds*1000),hours=Math.floor(ms/3600000),minutes=Math.floor(ms/60000)%60,whole=Math.floor(ms/1000)%60,fraction=ms%1000;
    const prefix=alwaysHours||hours?`${String(hours).padStart(2,'0')}:${String(minutes).padStart(2,'0')}`:String(minutes);
    return `${prefix}:${String(whole).padStart(2,'0')}${fraction?'.'+String(fraction).padStart(3,'0').replace(/0+$/,''):''}`;
  }
  function parseTime(value){
    const m=/^(?:(\d+):)?(\d{1,2}):([0-5]\d)(?:\.(\d{1,3}))?$/.exec(value.trim());
    if(!m||Number(m[2])>59)return NaN;
    return Number(m[1]||0)*3600+Number(m[2])*60+Number(m[3])+Number((m[4]||'').padEnd(3,'0'))/1000;
  }
  root.LanReviewTime={formatTime,parseTime};
  if(typeof module!=='undefined')module.exports=root.LanReviewTime;
})(typeof globalThis!=='undefined'?globalThis:this);
