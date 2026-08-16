/* eslint-disable @typescript-eslint/no-explicit-any, react-hooks/exhaustive-deps */
import { useEffect, useMemo, useRef, useState } from 'react'
import { LightweightMcpApp } from './mcp'
import { useVirtualizer } from '@tanstack/react-virtual'
import './index.css'

type PlaylistSummary = { playlistId: string; title: string; count?: number | string }
type Track = { kind?: 'track'; videoId?: string; setVideoId?: string; title?: string; artists?: string; album?: string; duration?: string; thumbnail?: string; youtubeMusicUrl?: string; playable?: boolean }
type Playlist = PlaylistSummary & { kind?: 'playlist'; description?: string; privacy?: string; tracks: Track[]; thumbnail?: string; youtubeMusicUrl?: string }
type SearchResult = Track & { kind?: 'track' | 'playlist'; browseId?: string; playlistId?: string; resultType?: string }
type PlayerData = { view: 'player'; queue: Track[]; requestedCount: number; playlist?: Playlist }
type View = 'tracks' | 'search' | 'settings'

declare global { interface Window { openai?: { toolOutput?: unknown; callTool?: (name: string, args: Record<string, unknown>) => Promise<unknown> }; YT?: any; onYouTubeIframeAPIReady?: () => void } }

function structured(value: any): any {
  const result = value?.result ?? value
  return result?.structuredContent
    ?? result?.structured_content
    ?? result?.toolOutput
    ?? result?.content?.find?.((item: any) => item?.type === 'resource')?.resource?.structuredContent
    ?? result
}

let mcp: LightweightMcpApp | null = null
async function callTool(name: string, args: Record<string, unknown>) {
  if (mcp) return structured(await mcp.callServerTool(name, args))
  if (window.openai?.callTool) return structured(await window.openai.callTool(name, args))
  throw new Error('This host does not expose tool calls to the app.')
}

function mediaUrl(item: Track | Playlist | SearchResult) { if(item.youtubeMusicUrl)return item.youtubeMusicUrl;if('videoId' in item&&item.videoId)return 'https://music.youtube.com/watch?v='+item.videoId;const id='playlistId' in item?item.playlistId:undefined;return id?'https://music.youtube.com/playlist?list='+id:'' }
async function copyMediaUrl(item: Track | Playlist | SearchResult){const url=mediaUrl(item);if(url)await navigator.clipboard.writeText(url)}
function Artwork({ track }: { track: Track }) {
  const [failed, setFailed] = useState(false)
  useEffect(() => setFailed(false), [track.thumbnail])
  return track.thumbnail && !failed
    ? <img className="art" src={track.thumbnail} alt="" loading="lazy" decoding="async" onError={() => setFailed(true)} />
    : <span className="art placeholder">♪</span>
}

function MediaActions({item}:{item:Track|Playlist|SearchResult}){const url=mediaUrl(item);return <div className="media-actions"><button className="secondary" disabled={!url} onClick={()=>copyMediaUrl(item)}>Copy link</button>{url&&<a className="button-link" href={url} target="_blank" rel="noreferrer">Open in YouTube Music</a>}</div>}
function PlayerPanel({data,onClose}:{data:PlayerData;onClose:()=>void}){const playerRef=useRef<any>(null),mountRef=useRef<HTMLDivElement>(null);const[index,setIndex]=useState(0),[blocked,setBlocked]=useState(false),[playerError,setPlayerError]=useState('');const ids=data.queue.map(t=>t.videoId).filter(Boolean)as string[],idsKey=ids.join(','),current=data.queue[index]||data.queue[0];useEffect(()=>{let cancelled=false;const create=()=>{if(cancelled||!mountRef.current||!window.YT?.Player||!ids.length)return;playerRef.current=new window.YT.Player(mountRef.current,{width:'100%',height:'240',playerVars:{autoplay:1,playsinline:1},events:{onReady:(e:any)=>{setPlayerError('');e.target.cuePlaylist(ids)},onStateChange:(e:any)=>{if(e.data===1){setBlocked(false);setPlayerError('');const n=e.target.getPlaylistIndex();if(n>=0)setIndex(n)}},onAutoplayBlocked:()=>setBlocked(true),onError:(e:any)=>setPlayerError('YouTube could not play this embed (error '+e.data+'). Use Open in YouTube Music below.')}})};if(window.YT?.Player)create();else{const prior=window.onYouTubeIframeAPIReady;window.onYouTubeIframeAPIReady=()=>{prior?.();create()};if(!document.querySelector('script[src="https://www.youtube.com/iframe_api"]')){const s=document.createElement('script');s.src='https://www.youtube.com/iframe_api';document.head.appendChild(s)}}return()=>{cancelled=true;playerRef.current?.destroy?.();playerRef.current=null}},[idsKey]);return <div className="player-view"><div className="player-top"><button className="secondary" onClick={onClose}>Back to library</button><span>{index+1} of {data.queue.length}</span></div><div className="player-grid"><section className="now-playing"><div className="embed" ref={mountRef}/>{playerError&&<div className="player-error">{playerError}</div>}{blocked&&<button className="tap-play" onClick={()=>playerRef.current?.playVideo?.()}>Tap to play</button>}<div className="now-meta"><Artwork track={current}/><div><span className="eyebrow">NOW PLAYING</span><h2>{current?.title||'Loading'}</h2><p>{current?.artists}</p></div></div><div className="player-controls"><button className="secondary" onClick={()=>playerRef.current?.previousVideo?.()}>Previous</button><button onClick={()=>playerRef.current?.playVideo?.()}>Play</button><button className="secondary" onClick={()=>playerRef.current?.pauseVideo?.()}>Pause</button><button className="secondary" onClick={()=>playerRef.current?.nextVideo?.()}>Next</button></div>{current&&<MediaActions item={current}/>}</section><section className="queue"><h3>Up next</h3>{data.queue.map((t,i)=><button className={i===index?'queue-row active':'queue-row'} key={(t.videoId||'track')+'-'+i} onClick={()=>playerRef.current?.playVideoAt?.(i)}><span>{i<data.requestedCount?i+1:'R'}</span><Artwork track={t}/><span><b>{t.title}</b><small>{t.artists}{i>=data.requestedCount?' - Recommended':''}</small></span></button>)}</section></div></div>}
function CardGallery({results,onClose,onPlay}:{results:SearchResult[];onClose:()=>void;onPlay:(item:SearchResult)=>void}){return <div className="card-view"><div className="player-top"><button className="secondary" onClick={onClose}>Back to library</button><h2>Music results</h2></div><div className="card-grid">{results.map((item,i)=><article className="media-card" key={item.videoId||item.playlistId||i}><button className="art-button" onClick={()=>window.open(mediaUrl(item),'_blank')}><Artwork track={item}/></button><div><span className="eyebrow">{item.kind||item.resultType}</span><h3>{item.title}</h3><p>{item.artists||item.duration||'YouTube Music playlist'}</p></div><div className="card-buttons"><button disabled={!item.playable} onClick={()=>onPlay(item)}>Play</button><MediaActions item={item}/></div></article>)}</div></div>}
export default function App() {
  const [playlists, setPlaylists] = useState<PlaylistSummary[]>([])
  const [playlist, setPlaylist] = useState<Playlist | null>(null)
  const [view, setView] = useState<View>('tracks')
  const [filter, setFilter] = useState('')
  const [page, setPage] = useState(0)
  const [pageSize, setPageSize] = useState(50)
  const [search, setSearch] = useState('')
  const [searchType, setSearchType] = useState('songs')
  const [results, setResults] = useState<SearchResult[]>([])
  const [selectedTracks, setSelectedTracks] = useState<Set<string>>(new Set())
  const [selectedResults, setSelectedResults] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState('')
  const [notice, setNotice] = useState('')
  const [createOpen, setCreateOpen] = useState(false)
  const [playerData, setPlayerData] = useState<PlayerData | null>(null)
  const [cardResults, setCardResults] = useState<SearchResult[] | null>(null)
  const scrollRef = useRef<HTMLDivElement>(null)

  const run = async <T,>(label: string, action: () => Promise<T>): Promise<T | undefined> => {
    setBusy(label); setNotice('')
    try { return await action() }
    catch (error) { setNotice(error instanceof Error ? error.message : String(error)) }
    finally { setBusy('') }
  }

  const refreshPlaylists = async () => {
    const data = await run('Loading playlists…', () => callTool('list_my_playlists', { limit: 100 }))
    if (Array.isArray(data)) setPlaylists(data)
  }
  const openPlaylist = async (id: string) => {
    const data = await run('Loading playlist…', () => callTool('get_playlist', { playlist_id: id }))
    if (data?.tracks) { setPlaylist(data); setView('tracks'); setSelectedTracks(new Set()) }
  }
  const mutate = async (label: string, name: string, args: Record<string, unknown>, refresh = true) => {
    const ok = await run(label, () => callTool(name, args))
    if (ok !== undefined) {
      setNotice(label.replace(/…$/, '') + ' complete.')
      if (refresh && playlist) await openPlaylist(playlist.playlistId)
      await refreshPlaylists()
    }
  }

  const createPlaylist = async (title: string, description: string, privacy_status: string) => {
    const created: any = await run("Creating playlist…", () => callTool("create_playlist", { title, description, privacy_status }))
    if (created?.playlistId) { setCreateOpen(false); await refreshPlaylists(); await openPlaylist(created.playlistId) }
  }

  useEffect(() => {
    const accept = (raw: any) => {
      const data = structured(raw)
      if (Array.isArray(data)) setPlaylists(data)
      if(data?.view==='player'&&Array.isArray(data.queue))setPlayerData(data)
      if(data?.view==='cards'&&Array.isArray(data.results))setCardResults(data.results)
      if (data?.tracks) setPlaylist(data)
      if (Array.isArray(data?.playlists)) setPlaylists(data.playlists)
    }
    accept(window.openai?.toolOutput)
    const app = new LightweightMcpApp(accept)
    app.connect().then(() => { mcp = app; refreshPlaylists() }).catch(() => refreshPlaylists())
    const globals = (event: Event) => accept((event as CustomEvent).detail?.globals?.toolOutput)
    window.addEventListener('openai:set_globals', globals)
    return () => window.removeEventListener('openai:set_globals', globals)
  }, [])

  const filtered = useMemo(() => {
    const q = filter.trim().toLowerCase()
    return (playlist?.tracks ?? []).map((track, index) => ({ track, index })).filter(({ track }) =>
      !q || [track.title, track.artists, track.album].some(v => v?.toLowerCase().includes(q)))
  }, [playlist, filter])
  const totalPages = Math.max(1, Math.ceil(filtered.length / pageSize))
  const currentPage = Math.min(page, totalPages - 1)
  const visible = useMemo(() => filtered.slice(currentPage * pageSize, (currentPage + 1) * pageSize), [filtered, currentPage, pageSize])
  useEffect(() => setPage(0), [filter, pageSize, playlist?.playlistId])
  const virtual = useVirtualizer({ count: visible.length, getScrollElement: () => scrollRef.current, estimateSize: () => 64, overscan: 6 })

  const submitSearch = async (event: React.FormEvent) => {
    event.preventDefault()
    const data = await run('Searching…', () => callTool('search_music', { query: search, filter: searchType, limit: 50 }))
    if (Array.isArray(data)) { setResults(data); setSelectedResults(new Set()) }
  }

  return <main className="app">
    <header className="topbar">
      <div><span className="brand">YT MUSIC</span><h1>Playlist manager</h1></div>
      <div className="playlist-controls">
        <select aria-label="Choose playlist" value={playlist?.playlistId || ''} onChange={e => e.target.value && openPlaylist(e.target.value)}>
          <option value="">Choose a playlist…</option>
          {playlists.map(p => <option key={p.playlistId} value={p.playlistId}>{p.title || 'Untitled'}{p.count !== undefined ? ` · ${p.count}` : ''}</option>)}
        </select>
        <div className="create-wrap"><button className="secondary icon-button" title="New playlist" onClick={() => setCreateOpen(!createOpen)}>＋</button>{createOpen && <CreateForm onCreate={createPlaylist} onClose={() => setCreateOpen(false)} />}</div>
        <button className="secondary icon-button" title="Refresh playlists" onClick={refreshPlaylists}>↻</button>
      </div>
    </header>
    {busy && playlist && <div className="status"><span className="spinner"/>{busy}</div>}
    {notice && <div className="notice" onClick={() => setNotice('')}>{notice}</div>}
    <div className="layout"><section className="content">
      {playerData?<PlayerPanel data={playerData} onClose={()=>setPlayerData(null)}/>:cardResults?<CardGallery results={cardResults} onClose={()=>setCardResults(null)} onPlay={async item=>{const data=item.videoId?await run('Building queue',()=>callTool('play_music',{video_ids:[item.videoId],include_recommendations:true})):await run('Loading playlist',()=>callTool('play_playlist',{playlist_id:item.playlistId,include_recommendations:true}));if(data?.queue)setPlayerData(data)}}/>:!playlist ? (busy ? <PlaylistSkeleton /> : <div className="empty"><div className="disc">♫</div><h2>Choose a playlist</h2><p>Use the selector above, or create a new one.</p></div>) : <>
        <div className="playlist-head"><div><span className="eyebrow">PLAYLIST</span><h2>{playlist.title || 'Untitled playlist'}</h2><p>{playlist.description || `${playlist.tracks.length} tracks · ${playlist.privacy || 'PRIVATE'}`}</p></div><div className="playlist-head-actions"><button disabled={!playlist.tracks.length} onClick={async()=>{const data=await run('Loading playlist',()=>callTool('play_playlist',{playlist_id:playlist.playlistId,include_recommendations:true}));if(data?.queue)setPlayerData(data)}}>Play playlist</button><MediaActions item={playlist}/></div></div>
        <div className="tabs"><button className={view === 'tracks' ? 'active' : ''} onClick={() => setView('tracks')}>Tracks</button><button className={view === 'search' ? 'active' : ''} onClick={() => setView('search')}>Add music</button><button className={view === 'settings' ? 'active' : ''} onClick={() => setView('settings')}>Settings</button></div>
        {view === 'tracks' && <div className="panel tracks-panel">
          <div className="toolbar"><input value={filter} onChange={e => setFilter(e.target.value)} placeholder="Filter tracks…"/><span>{filtered.length} tracks</span><label className="page-size">Show <select value={pageSize} onChange={e => setPageSize(Number(e.target.value))}>{[25,50,100,250].map(size => <option key={size} value={size}>{size}</option>)}</select></label>{selectedTracks.size > 0 && <button className="danger" onClick={() => { if (confirm(`Remove ${selectedTracks.size} selected track(s)?`)) mutate('Removing tracks…', 'remove_tracks_from_playlist', { playlist_id: playlist.playlistId, set_video_ids: [...selectedTracks] }) }}>Remove {selectedTracks.size}</button>}</div>
          <div className="track-scroll" ref={scrollRef}><div style={{ height: virtual.getTotalSize(), position: 'relative' }}>{virtual.getVirtualItems().map(row => {
            const { track, index } = visible[row.index]; const id = track.setVideoId || `${track.videoId}-${index}`
            return <div className="track" key={id} style={{ transform: `translateY(${row.start}px)` }}>
              <input type="checkbox" checked={selectedTracks.has(id)} disabled={!track.setVideoId} onChange={() => setSelectedTracks(toggle(selectedTracks, id))}/><span className="number">{index + 1}</span><Artwork track={track}/>
              <div className="track-main"><b>{track.title || 'Unknown track'}</b><small>{track.artists || 'Unknown artist'}{track.album ? ` · ${track.album}` : ''}</small></div><small>{track.duration}</small>
              <div className="row-actions"><button disabled={index === 0 || !track.setVideoId} title="Move up" onClick={() => mutate('Moving track…', 'reorder_playlist_track', { playlist_id: playlist.playlistId, set_video_id: track.setVideoId, before_set_video_id: playlist.tracks[index - 1]?.setVideoId })}>↑</button><button disabled={index === playlist.tracks.length - 1 || !track.setVideoId} title="Move down" onClick={() => mutate('Moving track…', 'reorder_playlist_track', { playlist_id: playlist.playlistId, set_video_id: playlist.tracks[index + 1]?.setVideoId, before_set_video_id: track.setVideoId })}>↓</button><div className="more-wrap"><button title="More track actions">•••</button><div className="more-menu"><button disabled={!track.videoId} onClick={async()=>{const data=await run('Building queue',()=>callTool('play_music',{video_ids:[track.videoId],include_recommendations:true}));if(data?.queue)setPlayerData(data)}}>Play now</button><a href={mediaUrl(track)} target="_blank" rel="noreferrer">Open in YT Music</a><button onClick={()=>copyMediaUrl(track)}>Copy link</button><button onClick={() => mutate('Moving track…', 'move_track_to_top', { playlist_id: playlist.playlistId, set_video_id: track.setVideoId })}>Move to top</button><button onClick={() => mutate('Moving track…', 'move_track_to_bottom', { playlist_id: playlist.playlistId, set_video_id: track.setVideoId })}>Move to bottom</button></div></div></div>
            </div>})}</div></div>
          <div className="pagination"><span>{filtered.length ? `${currentPage * pageSize + 1}–${Math.min((currentPage + 1) * pageSize, filtered.length)} of ${filtered.length}` : 'No tracks'}</span><div><button className="secondary" disabled={currentPage === 0} onClick={() => setPage(value => Math.max(0, value - 1))}>Previous</button><span>Page {currentPage + 1} of {totalPages}</span><button className="secondary" disabled={currentPage >= totalPages - 1} onClick={() => setPage(value => Math.min(totalPages - 1, value + 1))}>Next</button></div></div>
        </div>}
        {view === 'search' && <SearchPanel {...{search, setSearch, searchType, setSearchType, results, selectedResults, setSelectedResults, submitSearch}} onAdd={(top: boolean, duplicates: boolean) => mutate('Adding tracks…', 'add_tracks_to_playlist', { playlist_id: playlist.playlistId, video_ids: [...selectedResults], add_to_top: top, allow_duplicates: duplicates })}/>} 
        {view === 'settings' && <Settings playlist={playlist} onSave={(title, description, privacy_status) => mutate('Saving playlist…', 'rename_playlist', { playlist_id: playlist.playlistId, title, description, privacy_status })} onDelete={() => { if (confirm(`Permanently delete “${playlist.title}”?`)) mutate('Deleting playlist…', 'delete_playlist', { playlist_id: playlist.playlistId }, false).then(() => setPlaylist(null)) }}/>} 
      </>}
    </section></div>
  </main>
}

function toggle(set: Set<string>, id: string) { const next = new Set(set); if (next.has(id)) next.delete(id); else next.add(id); return next }
function PlaylistSkeleton() { return <div className="skeleton-view"><div className="skeleton title-skeleton"/><div className="skeleton tab-skeleton"/>{Array.from({length:7},(_,i)=><div className="skeleton-row" key={i}><span className="skeleton square"/><span><i className="skeleton line wide"/><i className="skeleton line"/></span></div>)}</div> }
function CreateForm({ onCreate, onClose }: { onCreate: (t: string, d: string, p: string) => void; onClose: () => void }) {
  const [title, setTitle] = useState(''); return <form className="create-popover" onSubmit={e => { e.preventDefault(); if (title.trim()) onCreate(title.trim(), '', 'PRIVATE') }}><div><b>New playlist</b><button type="button" className="close-button" onClick={onClose}>×</button></div><input value={title} onChange={e => setTitle(e.target.value)} placeholder="Playlist name" autoFocus/><button>Create playlist</button></form>
}
function SearchPanel(props: any) {
  const [top, setTop] = useState(false), [dupes, setDupes] = useState(false)
  return <div className="panel"><form className="searchbar" onSubmit={props.submitSearch}><input value={props.search} onChange={(e:any) => props.setSearch(e.target.value)} placeholder="Search YouTube Music…" required/><select value={props.searchType} onChange={(e:any) => props.setSearchType(e.target.value)}>{['songs','videos','albums','artists','playlists'].map(x => <option key={x}>{x}</option>)}</select><button>Search</button></form><div className="options"><label><input type="checkbox" checked={top} onChange={e => setTop(e.target.checked)}/> Add to top</label><label><input type="checkbox" checked={dupes} onChange={e => setDupes(e.target.checked)}/> Allow duplicates</label><button disabled={!props.selectedResults.size} onClick={() => props.onAdd(top, dupes)}>Add selected ({props.selectedResults.size})</button></div><div className="results">{props.results.map((r: SearchResult, i: number) => { const id = r.videoId || ''; return <label className="result" key={`${id}-${i}`}><input type="checkbox" disabled={!id} checked={props.selectedResults.has(id)} onChange={() => props.setSelectedResults(toggle(props.selectedResults, id))}/><Artwork track={r}/><span><b>{r.title}</b><small>{r.artists || r.resultType}</small></span><small>{r.duration}</small></label> })}</div></div>
}
function Settings({ playlist, onSave, onDelete }: { playlist: Playlist; onSave: (t:string,d:string,p:string)=>void; onDelete:()=>void }) {
  const [title,setTitle]=useState(playlist.title), [description,setDescription]=useState(playlist.description||''), [privacy,setPrivacy]=useState(playlist.privacy||'PRIVATE')
  useEffect(()=>{setTitle(playlist.title);setDescription(playlist.description||'');setPrivacy(playlist.privacy||'PRIVATE')},[playlist.playlistId])
  return <form className="panel settings" onSubmit={e=>{e.preventDefault();onSave(title,description,privacy)}}><label>Title<input value={title} onChange={e=>setTitle(e.target.value)} required/></label><label>Description<textarea value={description} onChange={e=>setDescription(e.target.value)}/></label><label>Privacy<select value={privacy} onChange={e=>setPrivacy(e.target.value)}>{['PRIVATE','UNLISTED','PUBLIC'].map(x=><option key={x}>{x}</option>)}</select></label><div><button>Save changes</button><button type="button" className="danger" onClick={onDelete}>Delete playlist</button></div></form>
}
