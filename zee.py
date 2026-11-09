#!/usr/bin/env python3
"""ZEE v0.13.1: consolidação Zeeschuimer e rede de interações do X.

Requisitos: Python >=3.10; pip install networkx plotly
Execute sem parâmetros para o menu; --help para comandos.
Inclui metadados (soma, ranking de posts e hashtags). Inclui dashboard HTML de volume diário de postagens.
"""
import argparse
import math
import csv
import shutil
import os
import tempfile
import hashlib
import json
import re
import sys
import traceback
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import urlparse
import networkx as nx
# Plotly é carregado somente quando a opção Timeline é executada.

TWITTER_COLUMNS = (
    'collected_from_url', 'id', 'thread_id', 'timestamp', 'unix_timestamp',
    'link', 'body', 'author', 'author_fullname', 'author_id', 'author_avatar_url',
    'author_banner_url', 'author_followers', 'author_following', 'author_bio',
    'author_location', 'verified', 'source', 'language_guess',
    'possibly_sensitive', 'retweet_count', 'reply_count', 'like_count',
    'quote_count', 'impression_count', 'is_retweet', 'retweeted_user',
    'is_quote_tweet', 'quote_tweet_id', 'quote_author', 'quote_body',
    'quote_images', 'quote_videos', 'is_quote_withheld', 'is_reply',
    'replied_author', 'is_withheld', 'hashtags', 'urls', 'images', 'videos',
    'mentions', 'long_lat', 'place_name',
)
REQUIRED = {'id','timestamp','link','author','author_id','is_retweet',
            'retweeted_user','is_quote_tweet','quote_author','is_reply',
            'replied_author','mentions'}
EDGE_TYPES = ('retweet','reply','quote','mention')
STATUS_RE = re.compile(r'/status/(\d+)',re.I)
HANDLE_RE = re.compile(r'^@?([A-Za-z0-9_]{1,15})$')
SCI_RE = re.compile(r'^\d+[.,]\d+[eE][+]?\d+$')


def detectar_delimitador(path):
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        sample = f.readline()
    if not sample:
        raise ValueError(f'CSV vazio: {path}')
    options=[]
    for sep in (';',',','\t'):
        names=[x.strip() for x in next(csv.reader([sample],delimiter=sep),[])]
        options.append((sum(x in TWITTER_COLUMNS or x in {'repost_count','hashtag','soma_count'} for x in names),len(names),sep))
    known,_,sep=max(options)
    if known < 2:
        raise ValueError(f'Cabeçalho do X não reconhecido: {path}')
    return sep


def csv_headers(path):
    sep=detectar_delimitador(path)
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        names=[x.strip() for x in next(csv.reader(f,delimiter=sep),[])]
    if len(names)!=len(set(names)):
        raise ValueError(f'Colunas duplicadas em {path}')
    return names


def reader(path):
    sep=detectar_delimitador(path)
    with open(path,'r',encoding='utf-8-sig',newline='') as f:
        parsed=csv.DictReader(f,delimiter=sep,restkey='__EXTRAS__',restval=None)
        names=[n.strip() for n in (parsed.fieldnames or [])]
        if not names or len(names)!=len(set(names)):
            raise ValueError(f'Cabeçalho ausente ou duplicado: {path}')
        for row in parsed:
            if row.get('__EXTRAS__') is not None or any(v is None for k,v in row.items() if k!='__EXTRAS__'):
                raise ValueError(f'Linha malformada: {path}, linha física próxima a {parsed.line_num}')
            yield {k.strip():v for k,v in row.items() if k!='__EXTRAS__'}


def post_id(row):
    m=STATUS_RE.search(row.get('link',''))
    return m.group(1) if m else row.get('id','')


def key_of(row):
    pid=post_id(row)
    if pid and not SCI_RE.match(pid):
        return ('post',pid)
    if row.get('link'):
        return ('link',row['link'])
    return None


def truthy(value):
    return str(value).strip().lower() in {'yes','true','sim','1','y'}


def handle(value):
    s=(value or '').strip()
    if s.startswith(('http://','https://')):
        parsed=urlparse(s)
        if parsed.hostname not in {'x.com','www.x.com','twitter.com','www.twitter.com','mobile.twitter.com'}:
            return ''
        s=parsed.path.strip('/').split('/')[0]
    m=HANDLE_RE.fullmatch(s)
    return m.group(1).lower() if m else ''


def mentions(value):
    if not value: return []
    s=value.strip()
    if s.startswith('['):
        try:
            parsed=json.loads(s)
            vals=([v.get('screen_name',v.get('username','')) if isinstance(v,dict) else v
                   for v in parsed] if isinstance(parsed,list) else [])
        except json.JSONDecodeError:
            vals=re.split(r'[,;\s]+',s)
    else:
        vals=re.split(r'[,;\s]+',s)
    return sorted({h for v in vals if (h:=handle(str(v)))})


def parse_timestamp(value, unix_value=''):
    """Obtém uma data UTC confiável de timestamp ou unix_timestamp.

    Prioridades: ISO com fuso -> unix_timestamp numérico -> aviso.
    NÃO presume um fuso para horários locais sem indicação.
    Números Unix são segundos, com suporte a milissegundos inteiros.
    """
    s=(value or '').strip()
    reason='ausente' if not s else 'invalido'
    if s:
        try:
            date=datetime.fromisoformat(s.replace('Z','+00:00'))
            if date.tzinfo is not None and date.utcoffset() is not None:
                return date.astimezone(timezone.utc),'timestamp_iso'
            reason='sem_fuso'
        except ValueError:
            # Alguns exportadores usam formatos não ISO. Não adivinhar o fuso.
            reason='invalido'
    u=(unix_value or '').strip()
    if u:
        try:
            value_decimal=Decimal(u)
            if not value_decimal.is_finite():
                raise ValueError('número Unix não finito')
            # 13 dígitos inteiros significam normalmente milissegundos Unix.
            # Não converter IDs de postagem nem números malformados.
            if value_decimal == value_decimal.to_integral_value() and abs(value_decimal)>=Decimal('100000000000'):
                value_decimal /= Decimal(1000)
            date=datetime.fromtimestamp(float(value_decimal), tz=timezone.utc)
            return date,'unix_timestamp'
        except (InvalidOperation, ValueError, OverflowError, OSError):
            return None,reason+'_unix_invalido'
    return None,reason


def summarize_file(path):
    p=Path(path)
    digest=hashlib.sha256()
    with p.open('rb') as f:
        for chunk in iter(lambda:f.read(1024*1024),b''):
            digest.update(chunk)
    return {'arquivo':str(p.resolve()), 'bytes':p.stat().st_size,
            'sha256':digest.hexdigest()}


class RunLogger:
    def __init__(self,path):
        self.path=Path(path)
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.f=self.path.open('w',encoding='utf-8')
    def say(self,message,level='INFO'):
        line=f'[{datetime.now(timezone.utc).isoformat(timespec="seconds")}] [{level}] {message}'
        print(line)
        self.f.write(line+'\n'); self.f.flush()
    def close(self):
        self.f.close()


def unique_run_dir(base,kind):
    base=Path(base)
    stamp=datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    folder=base/f'{kind}_{stamp}'
    folder.mkdir(parents=True,exist_ok=False)
    return folder


def save_report(folder, info, logger):
    dest=Path(folder)/'relatorio_processamento.txt'
    sections=[]
    for heading,values in info.items():
        sections += [heading.upper(),'-'*len(heading)]
        if isinstance(values,dict):
            sections.extend(f'{k}: {json.dumps(v,ensure_ascii=False) if isinstance(v,(dict,list)) else v}' for k,v in values.items())
        else:
            sections.append(str(values))
        sections.append('')
    dest.write_text('\n'.join(sections),encoding='utf-8')
    logger.say(f'Relatório salvo: {dest}')
    return dest


def merge(folder, output, recursive=False, log=None):
    """Une apenas o esquema original; contabiliza duplicatas por arquivo."""
    folder,output=Path(folder),Path(output)
    files=sorted(folder.rglob('*.csv') if recursive else folder.glob('*.csv'))
    files=[f for f in files if f.is_file() and f.resolve()!=output.resolve()]
    if not files: raise ValueError('Nenhum arquivo CSV encontrado.')
    expected=set(TWITTER_COLUMNS)
    for f in files:
        found=csv_headers(f)
        if set(found)!=expected or len(found)!=len(TWITTER_COLUMNS):
            raise ValueError(f'Esquema incompatível: {f.name}; faltantes={sorted(expected-set(found))}; extras={sorted(set(found)-expected)}')
    output.parent.mkdir(parents=True,exist_ok=True)
    tmp=output.with_name(output.name+'.tmp')
    seen=set(); per_file=[]; total=Counter(); duplicates_examples=[]; unidentified=0
    try:
        with tmp.open('w',encoding='utf-8-sig',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=TWITTER_COLUMNS,delimiter=';',lineterminator='\n')
            writer.writeheader()
            for f in files:
                c=Counter(); log.say(f'Lendo CSV: {f.name}; delimitador={repr(detectar_delimitador(f))}')
                for row in reader(f):
                    c['lidos']+=1; key=key_of(row)
                    if key is None:
                        unidentified+=1
                    elif key in seen:
                        c['duplicatas']+=1
                        if len(duplicates_examples)<10: duplicates_examples.append({'arquivo':f.name,'chave':str(key)})
                        continue
                    else:
                        seen.add(key)
                    writer.writerow(row); c['gravados']+=1
                total.update(c)
                per_file.append({'arquivo':f.name,**dict(c)})
                log.say(f'{f.name}: lidos={c["lidos"]}, duplicados={c["duplicatas"]}, gravados={c["gravados"]}')
        tmp.replace(output)
    finally:
        if tmp.exists(): tmp.unlink()
    return {'operacao':'merge','entrada':[summarize_file(f) for f in files],
            'saida':str(output),'linhas':dict(total),'por_arquivo':per_file,
            'duplicatas_amostra':duplicates_examples,
            'sem_chave_confiavel':unidentified,
            'aviso':'Linhas sem ID/link confiável são preservadas; duplicatas entre elas podem permanecer.'}



def clean_duplicates(source, folder, log, cleaning_mode='post'):
    """Gera novo CSV sem duplicatas, sem alterar o arquivo selecionado.

    post: mesma chave de publicação (key_of: ID/link), conservando linhas
          sem chave identificável, mesmo que visualmente idênticas.
    exact: todas as colunas e valores coincidem; não usa ID/link.
    Sempre preserva a PRIMEIRA ocorrência e a ordem de entrada.
    """
    if cleaning_mode not in ('post', 'exact'):
        raise ValueError(f'Modo de limpeza desconhecido: {cleaning_mode}')
    source = Path(source)
    headers = csv_headers(source)
    if cleaning_mode == 'post' and not ({'id', 'link'} & set(headers)):
        raise ValueError('Não há colunas id ou link. Use o modo exact para comparar linhas inteiras.')
    sep = detectar_delimitador(source)
    input_info = summarize_file(source)
    folder = Path(folder)
    dest = folder / (source.stem + '_limpo.csv')
    if dest.resolve() == source.resolve():
        raise ValueError('Arquivo de saída não pode substituir o arquivo de entrada.')
    seen = set()
    counts = Counter()
    duplicate_examples = []
    temp_path = None
    log.say('Limpeza iniciada: modo=' + cleaning_mode + '; entrada=' + str(source))
    try:
        # Arquivo temporário só é publicado depois que todas as linhas forem validadas.
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                                         prefix='.zee_limpeza_', suffix='.tmp',
                                         dir=folder, delete=False) as stream:
            temp_path = Path(stream.name)
            writer = csv.DictWriter(stream, fieldnames=headers, delimiter=sep,
                                    lineterminator='\n', extrasaction='raise')
            writer.writeheader()
            for line_number, row in enumerate(reader(source), 1):
                counts['linhas_lidas'] += 1
                if cleaning_mode == 'post':
                    identity = key_of(row)
                    if identity is None:
                        # Não inferir que dois posts são iguais sem identificador confiável.
                        counts['sem_chave_confiavel_preservadas'] += 1
                        writer.writerow(row)
                        counts['linhas_preservadas'] += 1
                        continue
                    token = identity
                else:
                    # Compara os valores lidos de todas as colunas, não a representação
                    # física da linha (quoting CSV pode variar sem mudar o conteúdo).
                    token = tuple(row.get(col, '') for col in headers)
                if token in seen:
                    counts['duplicatas_removidas'] += 1
                    if len(duplicate_examples) < 10:
                        duplicate_examples.append({
                            'registro': line_number,
                            'chave': str(token) if cleaning_mode == 'post'
                                     else '(linha inteira idêntica)'
                        })
                    continue
                seen.add(token)
                writer.writerow(row)
                counts['linhas_preservadas'] += 1
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_path, dest)
        temp_path = None
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink()
    counts['duplicatas_removidas'] += 0
    counts['sem_chave_confiavel_preservadas'] += 0
    log.say(f'Limpeza finalizada: lidas={counts["linhas_lidas"]}; '
            f'preservadas={counts["linhas_preservadas"]}; '
            f'removidas={counts["duplicatas_removidas"]}')
    log.say('CSV limpo salvo em: ' + str(dest))
    if counts['sem_chave_confiavel_preservadas']:
        log.say('Registros sem ID/link confiável preservados por segurança; '
                'use o modo exato para remover repetições idênticas dessas linhas.', 'WARN')
    return {
        'operacao': 'limpeza_duplicatas',
        'modo': cleaning_mode,
        'entrada': input_info,
        'saida': summarize_file(dest),
        'contagens': dict(counts),
        'duplicatas_amostra': duplicate_examples,
        'criterio': ('Mesmo ID da postagem ou link (função key_of); linhas sem chave confiável são mantidas.'
                     if cleaning_mode == 'post' else
                     'Igualdade de todas as colunas e valores após leitura CSV.'),
        'preservacao': 'Primeira ocorrência mantida; ordem das linhas e colunas preservada; origem inalterada.'
    }


def export_event_gexf(graph, events, path):
    """Cada edge GEXF 1.3 equivale a UM evento datado, com weight=1.

    O elemento timestamp (não um attvalue textual) define o instante do evento.
    Atributos adicionais preservam tipo e publicação. Mesmo timestamp repetido
    em eventos distintos NÃO os funde. Nós recebem spells por instante observado.
    """
    ns='http://gexf.net/1.3'
    ET.register_namespace('',ns)
    tag=lambda name: f'{{{ns}}}{name}'
    root=ET.Element(tag('gexf'), {'version':'1.3'})
    g=ET.SubElement(root,tag('graph'), {'mode':'dynamic','timerepresentation':'timestamp',
         'timeformat':'dateTime','defaultedgetype':'directed'})
    attrs=ET.SubElement(g,tag('attributes'),{'class':'edge','mode':'static'})
    for key,title,typ in [('0','interaction_type','string'),('1','post_id','string'),
                          ('2','timestamp_iso','string'),('3','unix_timestamp','long')]:
        ET.SubElement(attrs,tag('attribute'),{'id':key,'title':title,'type':typ})
    nodes=ET.SubElement(g,tag('nodes'))
    node_times=defaultdict(set)
    for e in events:
        node_times[e['source']].add(e['timestamp'])
        node_times[e['target']].add(e['timestamp'])
    for n in sorted(node_times):
        el=ET.SubElement(nodes,tag('node'),{'id':n,'label':'@'+n})
        spells=ET.SubElement(el,tag('spells'))
        for stamp in sorted(node_times[n]):
            ET.SubElement(spells,tag('spell'),{'timestamp':stamp})
    edges=ET.SubElement(g,tag('edges'))
    for i,e in enumerate(events):
        el=ET.SubElement(edges,tag('edge'),{'id':str(i),'source':e['source'],
              'target':e['target'],'kind':f'event_{i}', 'weight':'1', 'timestamp':e['timestamp']})
        values=ET.SubElement(el,tag('attvalues'))
        for key,value in [('0',e['type']),('1',e['post_id']),('2',e['timestamp']),
                          ('3',str(e['unix_timestamp']))]:
            ET.SubElement(values,tag('attvalue'),{'for':key,'value':value})
    ET.ElementTree(root).write(path,encoding='utf-8',xml_declaration=True)
    ET.parse(path)
    return {'nos_datados':len(node_times),'arestas_eventos_datados':len(events),
            'instantes_distintos':len({e['timestamp'] for e in events})}


def process(source,output,include_self_loops=False,log=None,mode='static'):
    """Rede de interações; estática agrega pares e temporal preserva eventos.

    Regra de peso da estática: eventos (inclui reply + mention do mesmo post).
    Rede temporal: cada aresta representa um evento específico do CSV e tem
    timestamp da publicação. Publicações sem data válida NÃO entram na dinâmica.
    """
    if mode not in ('static','temporal'):
        raise ValueError(f'Modo de rede desconhecido: {mode}')
    source,output=Path(source),Path(output)
    missing=REQUIRED-set(csv_headers(source))
    if missing: raise ValueError(f'Colunas obrigatórias ausentes: {sorted(missing)}')
    graph=nx.DiGraph(platform='Twitter/X',network_type='explicit_mentions',
                     direction_semantics='author_to_target',weight_semantics='number_of_events')
    counts=Counter(); kinds=Counter(); seen=set(); identity=defaultdict(set)
    time_stats=Counter(); bad_dates=[]; duplicates=[]; invalid_targets=[]
    dated_events=[]; all_events=[]
    for row in reader(source):
        counts['publicacoes_lidas']+=1
        key=key_of(row)
        if key is not None:
            if key in seen:
                counts['duplicatas_ignoradas']+=1
                if len(duplicates)<10: duplicates.append(str(key))
                continue
            seen.add(key)
        else:
            counts['publicacoes_sem_chave']+=1
        actor=handle(row.get('author',''))
        if not actor:
            counts['sem_autor_valido']+=1
            continue
        uid=(row.get('author_id') or '').strip()
        if uid and not SCI_RE.match(uid): identity[actor].add(uid)
        elif uid: counts['author_ids_notacao_cientifica']+=1
        if actor not in graph:
            graph.add_node(actor,label='@'+actor,username=actor,node_type='user')
        node=graph.nodes[actor]
        for col,attr in [('author_fullname','fullname'),('author_followers','followers'),
                         ('author_following','following'),('verified','verified')]:
            if row.get(col): node[attr]=str(row[col])
        if uid and not SCI_RE.match(uid): node['author_id']=uid
        # Esta modalidade usa SOMENTE a coluna mentions. Respostas, citações
        # e repostagens NÃO são reinterpretados como menções implícitas.
        candidates=[('mention',h) for h in mentions(row.get('mentions',''))]
        date,source_of_time=parse_timestamp(row.get('timestamp',''),row.get('unix_timestamp',''))
        time_stats[source_of_time]+=1
        if date is None and len(bad_dates)<10:
            bad_dates.append({'post_id':post_id(row),'timestamp':row.get('timestamp',''),
                              'unix_timestamp':row.get('unix_timestamp',''),'motivo':source_of_time})
        stamp=date.isoformat(timespec='microseconds').replace('+00:00','Z') if date else ''
        epoch=int(date.timestamp()) if date else None
        for kind,raw in candidates:
            target=handle(raw)
            if not target:
                counts['alvos_invalidos']+=1
                if len(invalid_targets)<10: invalid_targets.append({'tipo':kind,'valor':str(raw)[:80]})
                continue
            if target==actor and not include_self_loops:
                counts['autolacos_descartados']+=1
                continue
            if target not in graph:
                graph.add_node(target,label='@'+target,username=target,node_type='user')
            if not graph.has_edge(actor,target):
                graph.add_edge(actor,target,weight=0,retweet_interactions=0,
                       reply_interactions=0,quote_interactions=0,mention_interactions=0)
            edge=graph[actor][target]
            edge['weight']+=1
            edge[f'{kind}_interactions']+=1
            counts['eventos_observados']+=1
            kinds[kind]+=1
            event={'source':actor,'target':target,'type':kind,'post_id':post_id(row),
                   'timestamp':stamp,'unix_timestamp':epoch,'date_origin':source_of_time}
            all_events.append(event)
            if date:
                dated_events.append(event)
                counts['eventos_com_data_valida']+=1
            else:
                counts['eventos_sem_data_valida']+=1
    ambiguous={h:sorted(ids) for h,ids in identity.items() if len(ids)>1}
    for h,ids in identity.items():
        if h in graph:
            graph.nodes[h]['identity_status']='conflicting_author_ids' if len(ids)>1 else 'author_id_observed'
            if len(ids)>1: graph.nodes[h]['author_id']=''
    output.parent.mkdir(parents=True,exist_ok=True)
    paths={}
    if mode=='static':
        dest=output.with_suffix('.gexf')
        nx.write_gexf(graph,dest,encoding='utf-8',prettyprint=True)
        paths['gexf_estatico']=str(dest)
        log.say('Rede estática agregada salva: '+str(dest))
    else:
        if not dated_events:
            raise ValueError('Nenhum evento com data válida. Não é possível gerar uma rede temporal.')
        dest=output.with_name(output.name+'_temporal.gexf')
        stats=export_event_gexf(graph,dated_events,dest)
        paths['gexf_temporal']=str(dest)
        log.say('Rede temporal por evento salva: '+str(dest))
        log.say('Eventos com data: '+str(len(dated_events))+'; instantes: '+str(stats['instantes_distintos']))
    events_csv=output.with_name(output.name+'_eventos.csv')
    with events_csv.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=['source','target','type','post_id',
               'timestamp','unix_timestamp','date_origin'],delimiter=';')
        writer.writeheader(); writer.writerows(all_events)
    paths['csv_eventos']=str(events_csv)
    data={'metadata':{'platform':'Twitter/X','mode':mode,'source_csv':source.name,
                      'counts':dict(counts),'interactions_by_type':dict(kinds)},
          'nodes':[{'id':str(n),**a} for n,a in graph.nodes(data=True)],
          'links':[{'source':str(u),'target':str(v),**a} for u,v,a in graph.edges(data=True)]}
    json_dest=output.with_suffix('.json')
    json_dest.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    paths['json_rede_agregada']=str(json_dest)
    log.say(f'Rede: {graph.number_of_nodes()} nós, {graph.number_of_edges()} pares, '
            f'{counts["eventos_observados"]} eventos; {counts["eventos_sem_data_valida"]} sem data.')
    if ambiguous: log.say(f'{len(ambiguous)} perfis com IDs contraditórios.','WARN')
    if bad_dates: log.say('Ocorreram datas não interpretáveis; consultar relatório.','WARN')
    return {'operacao':'process','modo':mode,'entrada':summarize_file(source),
       'saidas':paths,'contagens':dict(counts),'interacoes_por_tipo':dict(kinds),
       'rede_agregada_referencia':{'nos':graph.number_of_nodes(),
              'pares_direcionados':graph.number_of_edges()},
       'datas':{'origens':dict(time_stats),'datas_invalidas_amostra':bad_dates,
           'regra':'Data do post por evento: ISO com fuso; fallback unix_timestamp; UTC. '
                    'Eventos sem data ficam no CSV de eventos, mas não na rede dinâmica. '
                    'Nenhuma primeira/última ocorrência é utilizada como tempo do evento.'},
       'auditoria':{'duplicatas_amostra':duplicates,'alvos_invalidos_amostra':invalid_targets,
          'conflitos_handle_author_id':ambiguous},
       'limites':{'escopo':'Arestas exclusivamente de menções explícitas na coluna mentions; respostas, citações e repostagens não são contadas.',
          'identidade':'Destino identificado por handle, não por ID persistente.',
          'evento':'Uma mesma publicação pode produzir eventos de tipos diferentes.',
          'temporal':'Cada aresta do GEXF temporal representa um evento de peso 1; '
                     'a Timeline pode exibir instantes ou seleção acumulada conforme sua janela.'}}

#  METADADOS: preservam a ordem e os valores das outras colunas.
#  Datas, hashtags e contagens são atributos de publicações, não da rede.

def parse_count(raw, column, rownum):
    """Aceita inteiros não negativos. Vazio equivale a zero e é auditado."""
    value = str(raw or '').strip()
    if not value:
        return 0, True
    # Não converte valores fracionários nem arredonda números inválidos.
    if not re.fullmatch(r'[0-9]+', value):
        raise ValueError(f'Registro {rownum}: {column}={value!r} não é inteiro não negativo.')
    return int(value), False


def metadata_sum(source, folder, log):
    """Inclui/atualiza soma_count NO CSV selecionado, com backup e troca atômica."""
    source = Path(source)
    headers = csv_headers(source)
    repost_column = 'repost_count' if 'repost_count' in headers else 'retweet_count'
    required = {'reply_count', 'like_count', repost_column}
    if not required.issubset(headers):
        raise ValueError(f'Colunas ausentes para soma: {sorted(required - set(headers))}.')
    sep = detectar_delimitador(source)
    input_stats = summarize_file(source)
    log.say('Iniciando soma_count; colunas: reply_count + like_count + ' + repost_column)
    tmp_path = None
    sums=[]; stats=Counter()
    fieldnames = headers if 'soma_count' in headers else headers + ['soma_count']
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8-sig', newline='',
                                         prefix='.zee_soma_', suffix='.tmp',
                                         dir=source.parent, delete=False) as stream:
            tmp_path=Path(stream.name)
            writer=csv.DictWriter(stream, fieldnames=fieldnames, delimiter=sep,
                                  lineterminator='\n', extrasaction='raise')
            writer.writeheader()
            for i, row in enumerate(reader(source), 1):
                amounts=[]
                for col in ('reply_count', 'like_count', repost_column):
                    number, empty = parse_count(row.get(col,''), col, i)
                    amounts.append(number)
                    stats['celulas_vazias_tratadas_como_zero']+=int(empty)
                total=sum(amounts)
                row['soma_count']=str(total)
                writer.writerow(row)
                sums.append(total)
                stats['publicacoes']+=1
            stream.flush()
            os.fsync(stream.fileno())
        backup=source.with_name(source.name+'.bak')
        if backup.exists():
            backup=source.with_name(source.name + '.' + datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.bak')
        shutil.copy2(source, backup)
        log.say('Backup do arquivo original criado: '+str(backup))
        os.replace(tmp_path, source)
        tmp_path=None
        log.say(f'soma_count gravada no CSV original: {source}; {stats["publicacoes"]} linhas.')
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink()
    return {'operacao':'soma_simples','entrada_antes':input_stats,
            'entrada_depois':summarize_file(source),'backup':str(backup),
            'colunas_utilizadas':['reply_count','like_count',repost_column],
            'coluna_criada':'soma_count','resultado':dict(stats),
            'soma_total':sum(sums),'menor_soma':min(sums) if sums else None,
            'maior_soma':max(sums) if sums else None,
            'regra':'Soma de três contagens por linha; células vazias equivalem a zero; '
                    'valores não inteiros ou negativos causam falha sem alterar o arquivo original.'}


def metadata_rank(source, folder, log):
    """Exporta até 50 linhas com maior soma_count, mantendo todos os campos."""
    source=Path(source)
    headers=csv_headers(source)
    if 'soma_count' not in headers:
        raise ValueError('Coluna soma_count não encontrada. Execute antes Metadados > Soma Simples.')
    rows=[]
    for i,row in enumerate(reader(source),1):
        score, empty=parse_count(row.get('soma_count',''), 'soma_count', i)
        if empty:
            raise ValueError(f'Registro {i}: soma_count vazia. Execute a Soma Simples novamente.')
        rows.append((score,i,row))
    rows.sort(key=lambda x:(-x[0],x[1]))
    best=rows[:50]
    dest=folder/(source.stem+'_top50_posts.csv')
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=headers,delimiter=detectar_delimitador(source),lineterminator='\n')
        writer.writeheader()
        for score,i,row in best:
            writer.writerow(row)
    log.say(f'Ranqueamento de postagens: {len(best)} de {len(rows)} linhas -> {dest}')
    return {'operacao':'ranking_posts','entrada':summarize_file(source),
            'saida':str(dest),'publicacoes_lidas':len(rows),'publicacoes_exportadas':len(best),
            'maior_soma':best[0][0] if best else None,'menor_soma_top':best[-1][0] if best else None,
            'regra':'Ordem decrescente de soma_count. Empates preservam a ordem original. '
                    'Todas as colunas originais são preservadas, sem deduplicar posts.'}


def extract_hashtags(cell):
    """Extrai as tags de uma única célula, sem procurar hashtags no body."""
    raw=(cell or '').strip()
    if not raw: return []
    tokens=[]
    if raw.startswith('['):
        try:
            parsed=json.loads(raw)
            if isinstance(parsed,list):
                for item in parsed:
                    if isinstance(item,str): tokens.append(item)
                    elif isinstance(item,dict):
                        tokens.append(str(item.get('text') or item.get('tag') or item.get('hashtag') or ''))
            else:
                tokens=[raw]
        except json.JSONDecodeError:
            tokens=[raw]
    else:
        tokens=[raw]
    tags=[]
    for token in tokens:
        # Listas simples separadas por vírgula, ponto e vírgula, espaços ou #.
        for match in re.finditer(r'#?([^\W_][\w]*)', token, flags=re.UNICODE):
            tag=match.group(1).casefold()
            if tag: tags.append(tag)
    return tags


def metadata_hashtags(source,folder,log):
    source=Path(source)
    headers=csv_headers(source)
    column='hashtag' if 'hashtag' in headers else 'hashtags' if 'hashtags' in headers else None
    if column is None:
        raise ValueError('Não existe coluna hashtag nem hashtags no CSV.')
    counts=Counter(); stats=Counter()
    for row in reader(source):
        stats['linhas']+=1
        tags=extract_hashtags(row.get(column,''))
        if not tags: stats['linhas_sem_tags']+=1
        else: stats['linhas_com_tags']+=1
        counts.update(tags)
        stats['total_utilizacoes']+=len(tags)
    dest=folder/(source.stem+'_ranking_hashtags.csv')
    with dest.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.writer(f,delimiter=';',lineterminator='\n')
        writer.writerow(['hashtag','quantidade'])
        for tag,quantity in sorted(counts.items(),key=lambda kv:(-kv[1],kv[0])):
            writer.writerow(['#'+tag,quantity])
    log.say(f'Hashtags da coluna {column}: {len(counts)} distintas; '
            f'{stats["total_utilizacoes"]} utilizações -> {dest}')
    return {'operacao':'ranking_hashtags','entrada':summarize_file(source),
            'coluna_lida':column,'saida':str(dest),'contagens':dict(stats),
            'tags_distintas':len(counts),'dez_mais_frequentes':[['#'+t,n] for t,n in counts.most_common(10)],
            'regra':'Conta ocorrências de cada hashtag na coluna específica, inclusive repetições '
                    'dentro de uma mesma célula; sem deduplicar posts. '
                    'Ignora diferença entre maiúsculas/minúsculas; não examina body.'}



DASHBOARD_RANKING_PRESETS = {
    'all': (50, None),    # 50 posts e todas as hashtags
    '25': (25, 25),       # 25 posts e 25 hashtags
    '15': (15, 15),       # 15 posts e 15 hashtags (padrão)
}


def normalize_excluded_hashtag(raw):
    """Normaliza uma única hashtag para comparação com extract_hashtags()."""
    value = (raw or '').strip().removeprefix('#').casefold()
    if not value:
        return None
    if re.fullmatch(r'[^\W_][\w]*', value, flags=re.UNICODE) is None:
        raise ValueError('Informe somente uma hashtag, com ou sem #, sem espaços (ex.: #riograndedosul).')
    return value


def timeline_dashboard(source, folder, log, tz_name='America/Sao_Paulo', deduplicate=True, ranking_size='15', exclude_tag=None):
    """Dashboard offline: visão geral, séries dia/minuto, top posts e hashtags.

    Os dados ficam incorporados no HTML. Não gera CSVs auxiliares.
    Ranking dos posts usa soma_count quando válida; senão calcula soma das
    três colunas de engajamento. Estatísticas observadas no CSV, não na plataforma.
    """
    if ranking_size not in DASHBOARD_RANKING_PRESETS:
        raise ValueError('Perfil de ranking inválido. Escolha all, 25 ou 15.')
    posts_limit, tags_limit = DASHBOARD_RANKING_PRESETS[ranking_size]
    excluded_tag = normalize_excluded_hashtag(exclude_tag)
    try:
        import plotly.graph_objects as go
        from plotly.offline import get_plotlyjs
    except ImportError as exc:
        raise ValueError('O dashboard requer Plotly: pip install plotly') from exc
    from html import escape

    try:
        tz = ZoneInfo(tz_name)
    except (KeyError, ValueError) as exc:
        raise ValueError(f'Fuso inválido: {tz_name}') from exc
    headers = set(csv_headers(source))
    if not ({'timestamp', 'unix_timestamp'} & headers):
        raise ValueError('Não existe timestamp nem unix_timestamp no CSV.')
    tag_col = 'hashtag' if 'hashtag' in headers else 'hashtags' if 'hashtags' in headers else None
    repost_col = 'repost_count' if 'repost_count' in headers else 'retweet_count' if 'retweet_count' in headers else None

    days_count, minutes_count, tag_count, causes, date_sources = Counter(), Counter(), Counter(), Counter(), Counter()
    seen, authors = set(), set()
    top_posts = []
    total = duplicates = without_key = without_date = no_score = 0
    seq = 0
    for row in reader(source):
        total += 1
        identity = key_of(row)
        if deduplicate and identity is not None:
            if identity in seen:
                duplicates += 1
                continue
            seen.add(identity)
        if identity is None: without_key += 1
        seq += 1
        author = (row.get('author') or '').strip()
        author_fullname = (row.get('author_fullname') or '').strip()
        if author: authors.add(author.casefold())
        timestamp, origin = parse_timestamp(row.get('timestamp',''), row.get('unix_timestamp',''))
        local = timestamp.astimezone(tz) if timestamp else None
        if local:
            days_count[local.date()] += 1
            minutes_count[local.replace(second=0,microsecond=0)] += 1
            date_sources[origin] += 1
        else:
            without_date += 1
            causes[origin] += 1
        if tag_col:
            # Exclusão apenas do ranking; mantém os posts, datas e CSV de entrada.
            for tag in extract_hashtags(row.get(tag_col,'')):
                if tag != excluded_tag:
                    tag_count[tag] += 1
        try:
            if row.get('soma_count','').strip():
                score, _ = parse_count(row['soma_count'], 'soma_count', seq)
            elif repost_col and {'like_count','reply_count'}.issubset(headers):
                score = sum(parse_count(row.get(c,''),c,seq)[0] for c in ('like_count','reply_count',repost_col))
            else:
                score = None
            if score is None:
                no_score += 1
            else:
                top_posts.append({'score':score, 'seq':seq, 'author':author or '(sem autor)',
                    'author_fullname':author_fullname,
                    'body':row.get('body',''), 'link':row.get('link',''),
                    'datetime':local.isoformat(timespec='seconds') if local else '',
                    'likes':row.get('like_count',''), 'reposts':row.get(repost_col,'') if repost_col else '',
                    'replies':row.get('reply_count','')})
        except ValueError:
            no_score += 1
            causes['contagem_invalida'] += 1
    if not days_count:
        raise ValueError('Não há postagens com data válida para gerar a Timeline.')
    top_posts.sort(key=lambda item: (-item['score'],item['seq']))
    top_posts = top_posts[:posts_limit]
    tags_all = sorted(tag_count.items(),key=lambda item:(-item[1],item[0]))
    tags_sorted = tags_all if tags_limit is None else tags_all[:tags_limit]
    first,last = min(days_count),max(days_count)
    days_with_posts = len(days_count)
    span = (last-first).days + 1
    if span > 36600:
        raise ValueError('Intervalo superior a 100 anos; confira as datas.')
    dates = [first+timedelta(days=i) for i in range(span)]
    daily = [days_count.get(d,0) for d in dates]
    dated = sum(daily)
    peak = max(daily)
    peaks = [d.isoformat() for d,count in zip(dates,daily) if count==peak]
    minute_peak = max(minutes_count.values())
    day_minutes = defaultdict(dict)
    for dt,n in minutes_count.items():
        key = dt.date().isoformat()
        clock = dt.strftime('%H:%M')
        day_minutes[key][clock] = day_minutes[key].get(clock,0)+n

    def js_safe(data):
        return json.dumps(data,ensure_ascii=False,separators=(',',':')).replace('<','\\u003c').replace('>','\\u003e').replace('&','\\u0026')

    # Modelo B: linha fina com pontos circulares discretos nas duas séries diárias.
    fig = go.Figure(go.Scatter(x=[d.isoformat() for d in dates],y=daily,
         mode='lines+markers',line={'color':'#05B70B','width':1.3},
         marker={'symbol':'circle','size':4,'color':'#05B70B'},
         fill='tozeroy',fillcolor='rgba(5,183,11,.08)',name='Postagens',
         hovertemplate='%{x}<br><b>%{y}</b> postagens<extra></extra>'))
    fig.update_layout(template='plotly_white',height=360,margin=dict(l=55,r=20,t=20,b=50),
         xaxis=dict(title='Data de publicação',type='date',rangeslider={'visible':True},gridcolor='#dedede'),
         yaxis=dict(title='Postagens por dia',rangemode='tozero',gridcolor='#dedede'),
         paper_bgcolor='#f6f6f6',plot_bgcolor='#f6f6f6',font={'family':'SF Pro, SF Pro Display, Segoe UI, Arial, sans-serif'})
    daily_fragment = fig.to_html(full_html=False,include_plotlyjs=False,
                                  config={'displaylogo':False,'responsive':True})
    payload = js_safe({'minute':day_minutes, 'posts':top_posts,
                       'tags':[['#'+tag,n] for tag,n in tags_sorted],
                       'available_days':sorted(day_minutes), 'default_day':peaks[0]})
    # Interface reproduzida a partir do frame Figma 1:678, mantida fora da lógica de análise.
    # O HTML final incorpora CSS, JS e Plotly para continuar funcionando offline.
    ui_dir = Path(__file__).resolve().parent / 'dashboard'
    for required_ui_file in ('template.html', 'style.css', 'scripts.js'):
        if not (ui_dir / required_ui_file).is_file():
            raise FileNotFoundError(f'Arquivo da interface ausente: {ui_dir / required_ui_file}')
    html = (ui_dir / 'template.html').read_text(encoding='utf-8')
    css = (ui_dir / 'style.css').read_text(encoding='utf-8')
    scripts = (ui_dir / 'scripts.js').read_text(encoding='utf-8')
    scripts = scripts.replace('__DATA__', payload)
    # Estilização do gráfico por minuto, criado dinamicamente por scripts.js.
    # Interceptar as chamadas do Plotly evita exigir mudanças no template externo.
    # Todas as séries Scatter com linhas ganham pontos circulares; dados intactos.
    pixel_dot_style = r'''
(function () {
  'use strict';
  const plotly = window.Plotly;
  if (!plotly) return;
  function applyDotStyle(traces) {
    if (!Array.isArray(traces)) return;
    for (const trace of traces) {
      if (!trace || (trace.type && trace.type !== 'scatter' && trace.type !== 'scattergl')) continue;
      const mode = trace.mode || 'lines';
      if (mode.indexOf('lines') === -1) continue;
      trace.mode = 'lines+markers';
      // A cor dos círculos acompanha a cor original da linha.
      // Não forçar verde: a série por hora/minuto pode ser amarela.
      const originalLineColor = trace.line && trace.line.color;
      const originalMarkerColor = trace.marker && trace.marker.color;
      trace.line = Object.assign({}, trace.line, { width: 1.3 });
      const markerStyle = { symbol: 'circle', size: 3 };
      if (originalLineColor) markerStyle.color = originalLineColor;
      else if (originalMarkerColor) markerStyle.color = originalMarkerColor;
      trace.marker = Object.assign({}, trace.marker, markerStyle);
    }
  }
  for (const name of ['newPlot', 'react']) {
    const original = plotly[name];
    if (typeof original !== 'function') continue;
    plotly[name] = function (graph, data, layout, config) {
      applyDotStyle(data);
      return original.apply(this, arguments);
    };
  }
})();
'''
    scripts = pixel_dot_style + '\n' + scripts
    html = html.replace('__STYLE__', css).replace('__SCRIPT__', scripts)
    replacements = {
        '__TZ__':escape(tz_name), '__NPOSTS__':f'{seq:,}'.replace(',','.'),
        '__AUTHORS__':f'{len(authors):,}'.replace(',','.'),
        '__NTAGS__':f'{len(tag_count):,}'.replace(',','.'), '__NDAYS__':str(days_with_posts),
        '__FIRST__':first.isoformat(), '__LAST__':last.isoformat(),
        '__INVALID__':str(without_date),'__DUPLICATES__':str(duplicates),
        '__DATED__':str(dated),'__MEAN__':f'{dated/span:.1f}'.replace('.',','),
        '__PEAK__':str(peak), '__DAILY_OVERVIEW__':daily_fragment,
        '__DAILY_TIMELINE__':daily_fragment,
        '__DATA__':payload, '__PLOTLY__':get_plotlyjs()
    }
    # Cada fragmento do Plotly precisa de ID único (inclusive os scripts internos).
    # Construir figura separada é mais seguro que substituir apenas o atributo id.
    replacement_daily_2 = fig.to_html(full_html=False,include_plotlyjs=False,
                                      config={'displaylogo':False,'responsive':True})
    replacements['__DAILY_TIMELINE__']=replacement_daily_2
    for key,value in replacements.items():
        html=html.replace(key,value)
    # Adição não destrutiva ao template externo (Figma): exibe nome completo
    # sobre o @ no card, sem depender de alterar dashboard/scripts.js.
    # Textos são sempre adicionados via textContent, evitando HTML não confiável.
    fullnames_for_ui = js_safe([{'author':p['author'],
        'author_fullname':p['author_fullname']} for p in top_posts
        if p.get('author_fullname')])
    addon = r"""
<style>
.zee-author-fullname { display:block; font:inherit; font-weight:700; line-height:inherit; }
.zee-author-identity { display:flex; flex-direction:column; align-items:flex-start; min-width:0; }
</style>
<script>
(function () {
  const posts = __ZEE_FULLNAMES__;
  if (!posts.length) return;
  function update() {
    const root = document.querySelector('#postList, #postsList, #posts-list, .posts-list, .post-list') || document;
    const cards = root.querySelectorAll('article.post, article.post-card, .post-card, [data-post-card], article');
    for (const card of cards) {
      if (card.querySelector('.zee-author-fullname')) continue;
      const candidates = card.querySelectorAll('.post-head strong, .post-author, .author-handle, .author-name, strong');
      for (const target of candidates) {
        if (target.querySelector('.zee-author-fullname')) continue;
        const shown = (target.textContent || '').trim();
        // A correspondência é com o @ exibido, e não com o corpo da postagem.
        const shownHandle = shown.replace(/^\d+\.\s*/, '').replace(/^@/, '').toLowerCase();
        const post = posts.find(p => shownHandle === p.author.replace(/^@/, '').toLowerCase());
        if (!post || !post.author_fullname || post.author_fullname === shown) continue;
        const wrap = document.createElement('span');
        wrap.className = 'zee-author-identity';
        const fullname = document.createElement('strong');
        fullname.className = 'zee-author-fullname';
        fullname.textContent = post.author_fullname;
        target.parentNode.insertBefore(wrap, target);
        wrap.appendChild(fullname);
        wrap.appendChild(target);
        break;
      }
    }
  }
  // A aba de postagens pode ser criada ao ser aberta: observar novas inserções.
  let scheduled = false;
  const watcher = new MutationObserver(function () {
    if (scheduled) return;
    scheduled = true;
    queueMicrotask(function () { scheduled = false; update(); });
  });
  function init() {
    update();
    const list = document.querySelector('#postList, #postsList, #posts-list, .posts-list, .post-list');
    watcher.observe(list || document.body, {childList:true, subtree:true});
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', init);
  else init();
})();
</script>
""".replace('__ZEE_FULLNAMES__',fullnames_for_ui)
    if '</body>' in html:
        html = html.replace('</body>', addon + '</body>', 1)
    else:
        html += addon
    dest=Path(folder)/'dashboard.html'
    dest.write_text(html,encoding='utf-8')
    log.say(f'Dashboard HTML criado: {dest}')
    log.say(f'Publicações: {seq} únicas, {dated} datadas; duplicatas: {duplicates}; tags distintas após exclusão: {len(tag_count)}; posts exibidos: {len(top_posts)}; tags exibidas: {len(tags_sorted)}.')
    log.say(f'Ranking no dashboard: perfil={ranking_size}; hashtag excluída={"#"+excluded_tag if excluded_tag else "nenhuma"}.')
    log.say('Dashboard sem exportação de CSVs auxiliares; ranking de posts calculado quando soma_count está ausente.')
    return {
        'operacao':'dashboard_integrado', 'entrada':summarize_file(source),
        'arquivos_saida':{'dashboard_html':str(dest)},
        'parametros':{'timezone':tz_name,'deduplicacao':deduplicate,'unidade_tempo':'dia e minuto de publicação',
            'ranking_posts':'soma_count ou reply_count + like_count + repost_count/retweet_count',
            'ranking_hashtags':f'coluna {tag_col}' if tag_col else 'coluna não encontrada',
            'exportacao_csv_auxiliar':False,
            'perfil_ranking':ranking_size,'limite_posts':posts_limit,'limite_hashtags':tags_limit,
            'hashtag_excluida':'#'+excluded_tag if excluded_tag else None,
            'escopo_exclusao':'somente ranking de hashtags; demais métricas de postagens preservadas'},
        'contagens':{'linhas_lidas':total,'publicacoes_unicas':seq,'duplicatas_ignoradas':duplicates,
            'registros_sem_chave_confiavel':without_key,'postagens_datadas':dated,
            'datas_ausentes_invalidas':without_date,'autores_distintos':len(authors),
            'hashtags_distintas':len(tag_count),'posts_ranqueados':len(top_posts),
            'hashtags_exibidas':len(tags_sorted),'posts_exibidos':len(top_posts),
            'posts_sem_pontuacao_valida':no_score,'origem_datas':dict(date_sources),
            'avisos':dict(causes),'dias_com_postagens':days_with_posts,
            'dias_no_intervalo':span,'dias_sem_postagens_no_intervalo':span-days_with_posts},
        'resumo':{'primeiro_dia':first.isoformat(),'ultimo_dia':last.isoformat(),
            'datas_mais_antigas_observadas':[d.isoformat() for d in sorted(days_count)[:10]],
            'datas_mais_recentes_observadas':[d.isoformat() for d in sorted(days_count)[-10:]],
            'maior_volume_diario':peak,'dias_pico':peaks,'maior_volume_minuto':minute_peak,
            'mais_utilizadas':[['#'+tag,n] for tag,n in tags_sorted[:10]]},
        'observacao':'Datas representam a PUBLICAÇÃO, não a coleta. A interface funciona sem conexão à internet.'}

def execute(kind,inp,out_base,recursive=False,self_loops=False,mode='static',timezone_name='America/Sao_Paulo',cleaning_mode='post',ranking_size='15',exclude_tag=None):
    kind_dir = {'merge':'merge','process':'rede_mencoes_'+mode,
                'sum':'metadados_soma','rank':'metadados_ranking_posts',
                'hashtags':'metadados_ranking_hashtags', 'timeline':'timeline',
                 'clean':'limpeza_'+cleaning_mode}[kind]
    folder=unique_run_dir(out_base,kind_dir)
    log=RunLogger(folder/'execucao.log.txt')
    report={'execucao':{'operacao':kind,'modo':mode if kind=='process' else cleaning_mode if kind=='clean' else None,
            'inicio_utc':datetime.now(timezone.utc).isoformat(),'pasta':str(folder),
            'versao':'0.13.1','status':'EM_ANDAMENTO'}}
    try:
        log.say(f'INÍCIO: {kind}/{mode}; origem={inp}; destino={folder}')
        if kind=='merge':
            report['resultado']=merge(inp,folder/'twitter_consolidado.csv',recursive,log)
        elif kind=='process':
            report['resultado']=process(inp,folder/(Path(inp).stem+'_rede'),self_loops,log,mode)
        elif kind=='sum':
            report['resultado']=metadata_sum(inp,folder,log)
        elif kind=='rank':
            report['resultado']=metadata_rank(inp,folder,log)
        elif kind=='hashtags':
            report['resultado']=metadata_hashtags(inp,folder,log)
        elif kind=='timeline':
            report['resultado']=timeline_dashboard(inp,folder,log,tz_name=timezone_name,ranking_size=ranking_size,exclude_tag=exclude_tag)
        elif kind=='clean':
            report['resultado']=clean_duplicates(inp,folder,log,cleaning_mode=cleaning_mode)
        report['execucao']['status']='CONCLUÍDO'
        log.say('Execução concluída com sucesso.')
    except Exception as exc:
        report['execucao']['status']='FALHA'
        report['erro']={'tipo':type(exc).__name__,'mensagem':str(exc)}
        log.say(f'FALHA: {type(exc).__name__}: {exc}','ERROR')
        log.say(traceback.format_exc(),'ERROR')
        raise
    finally:
        report['execucao']['fim_utc']=datetime.now(timezone.utc).isoformat()
        save_report(folder,report,log)
        (folder/'configuracao.json').write_text(json.dumps({
            'operacao':kind,'modo':mode if kind=='process' else cleaning_mode if kind=='clean' else None,
            'entrada':str(inp),'recursive':recursive,'self_loops':self_loops,
            'pasta_saida':str(folder),'timezone':timezone_name,
            'cleaning_mode':cleaning_mode if kind=='clean' else None,
            'ranking_size':ranking_size if kind=='timeline' else None,
            'exclude_tag':'#'+normalize_excluded_hashtag(exclude_tag) if kind=='timeline' and normalize_excluded_hashtag(exclude_tag) else None},indent=2,ensure_ascii=False),encoding='utf-8')
        log.close()
    return folder

def listar_csvs(folder):
    return sorted((p for p in Path(folder).glob('*.csv') if p.is_file()),key=lambda p:p.name.casefold())


def escolher_csv(paths):
    if not paths:
        print('Nenhum CSV disponível.'); return None
    for i,f in enumerate(paths,1): print(f'  {i}. {f}')
    while True:
        ans=input('Número do CSV (0 = voltar): ').strip()
        if ans=='0': return None
        if ans.isdigit() and 1<=int(ans)<=len(paths): return paths[int(ans)-1]
        print('Escolha inválida.')


def menu_interativo():
    folder=Path(__file__).resolve().parent
    output=folder/'output'

    def all_csv():
        files=listar_csvs(folder)
        generated=sorted(output.glob('merge_*/twitter_consolidado.csv')) if output.exists() else []
        cleaned=sorted(output.glob('limpeza_*/*_limpo.csv')) if output.exists() else []
        return files+generated+cleaned

    while True:
        files=listar_csvs(folder)
        print('\n' + '='*54)
        print('            ZEE v0.13.1 — ZEESCHUIMER / X')
        print('='*54)
        print('\n  [1] Merge\n')
        print('  [2] Geração de Redes\n')
        print('  [3] Metadados\n')
        print('  [4] Timeline — postagens por dia e minuto\n')
        print('  [5] Limpeza — remover duplicatas\n')
        print('  [6] Sair\n')
        print('-'*54)
        try:
            choice=input('Escolha: ').strip()
            if choice=='6': return
            if choice=='1':
                if len(files)<2:
                    print('Coloque pelo menos dois CSVs na pasta do script.'); continue
                dest=execute('merge',folder,output)
                print(f'Resultados em: {dest}')
            elif choice=='2':
                while True:
                    print('\nGERAÇÃO DE REDES')
                    print('  [1] Rede de Menções - Estática\n')
                    print('  [2] Rede de Menções - Temporal\n')
                    print('  [0] Voltar\n')
                    selection=input('Escolha: ').strip()
                    if selection=='0': break
                    if selection not in ('1','2'):
                        print('Opção inválida.'); continue
                    selected=escolher_csv(all_csv())
                    if selected is None: continue
                    mode='static' if selection=='1' else 'temporal'
                    dest=execute('process',selected,output,mode=mode)
                    print(f'Resultados em: {dest}')
            elif choice=='3':
                while True:
                    print('\nMETADADOS')
                    print('  [1] Soma Simples — acrescentar soma_count ao CSV escolhido\n')
                    print('  [2] Ranqueamento Post — 50 maiores soma_count\n')
                    print('  [3] Ranqueamento Tag — frequência de hashtags\n')
                    print('  [0] Voltar\n')
                    selection=input('Escolha: ').strip()
                    if selection=='0': break
                    kinds={'1':'sum','2':'rank','3':'hashtags'}
                    if selection not in kinds:
                        print('Opção inválida.'); continue
                    selected=escolher_csv(all_csv())
                    if selected is None: continue
                    if selection=='1':
                        print(f'ATENÇÃO: a coluna soma_count será gravada no CSV selecionado:\n{selected}')
                        print('Será criado um backup .bak antes da substituição.')
                        confirmation=input('Confirmar alteração do CSV? [s/N]: ').strip().lower()
                        if confirmation not in ('s','sim'):
                            print('Operação cancelada.'); continue
                    dest=execute(kinds[selection],selected,output)
                    print(f'Resultados em: {dest}')
            elif choice=='4':
                selected=escolher_csv(all_csv())
                if selected is None: continue
                print('\nTimeline por DATA DE PUBLICAÇÃO (não data de coleta).')
                print('Fuso padrão: America/Sao_Paulo. Datas serão agrupadas por dia e minuto locais.')
                tz_choice=input('Fuso IANA [Enter = America/Sao_Paulo]: ').strip() or 'America/Sao_Paulo'
                print('\nRANQUEAMENTOS DO DASHBOARD')
                print('  [1] 50 postagens + todas as hashtags')
                print('  [2] 25 postagens + 25 hashtags')
                print('  [3] 15 postagens + 15 hashtags (padrão)')
                preset_choice=input('Escolha [Enter = 3]: ').strip() or '3'
                presets={'1':'all','2':'25','3':'15'}
                if preset_choice not in presets:
                    print('Opção inválida.'); continue
                excluded=input('Excluir alguma hashtag do ranking? [Enter = nenhuma; ex.: #riograndedosul]: ').strip()
                if excluded:
                    excluded=normalize_excluded_hashtag(excluded)
                    print(f'A hashtag #{excluded} será excluída apenas do ranking, não do dataset.')
                dest=execute('timeline',selected,output,timezone_name=tz_choice,
                             ranking_size=presets[preset_choice],exclude_tag=excluded)
                print(f'\nDashboard em: {dest}\n')
            elif choice=='5':
                print('\nLIMPEZA DE DUPLICATAS')
                print('  [1] Mesmo post (ID/link) — padrão\n')
                print('  [2] Linha inteira idêntica\n')
                print('  [0] Voltar\n')
                selection=input('Escolha: ').strip()
                if selection=='0': continue
                if selection not in ('1','2'):
                    print('Opção inválida.'); continue
                selected=escolher_csv(all_csv())
                if selected is None: continue
                selected_mode='post' if selection=='1' else 'exact'
                print('O CSV original NÃO será modificado; será criado um novo arquivo limpo.')
                dest=execute('clean',selected,output,cleaning_mode=selected_mode)
                print(f'Resultados em: {dest}')
            else:
                print('Opção inválida.')
        except (EOFError,KeyboardInterrupt):
            print('\nInterrompido.'); return
        except Exception as exc:
            print(f'Erro: {exc}. Confira o relatório e o log em output/.',file=sys.stderr)


def main():
    if len(sys.argv)==1: return menu_interativo()
    p=argparse.ArgumentParser(description='ZEE v0.13.1 — merge, redes de menções, metadados, Timeline e limpeza')
    sub=p.add_subparsers(dest='command',required=True)
    m=sub.add_parser('merge',help='Consolidar CSVs Zeeschuimer')
    m.add_argument('folder'); m.add_argument('-o','--output',required=True)
    m.add_argument('--recursive',action='store_true')
    q=sub.add_parser('process',help='Gerar rede de menções estática ou temporal')
    q.add_argument('csv'); q.add_argument('-o','--output',required=True)
    q.add_argument('--mode',choices=['static','temporal'],default='static')
    q.add_argument('--self-loops',action='store_true')
    for command,help_text in [('sum','Somar as contagens e alterar CSV com backup'),
                              ('rank','Exportar 50 posts por soma_count'),
                              ('hashtags','Exportar frequência de hashtags')]:
        item=sub.add_parser(command,help=help_text)
        item.add_argument('csv'); item.add_argument('-o','--output',required=True)
    c=sub.add_parser('clean',help='Criar CSV sem duplicatas (sem alterar a origem)')
    c.add_argument('csv'); c.add_argument('-o','--output',required=True)
    c.add_argument('--mode',dest='cleaning_mode',choices=['post','exact'],default='post',
                   help='post=mesma publicação por ID/link; exact=linha inteira idêntica')
    t=sub.add_parser('timeline',help='Dashboard HTML integrado (Timeline, Posts e Hashtags)')
    t.add_argument('csv'); t.add_argument('-o','--output',required=True)
    t.add_argument('--timezone',default='America/Sao_Paulo',help='Fuso IANA para agregação diária')
    t.add_argument('--ranking-size',choices=['all','25','15'],default='15',
                   help='all=50 posts/todas as tags; 25=25/25; 15=15/15 (padrão)')
    t.add_argument('--exclude-tag',default=None,
                   help='Remover uma hashtag apenas do ranking no dashboard (ex.: #riograndedosul)')
    args=p.parse_args()
    try:
        if args.command=='sum':
            print(f'ATENÇÃO: o CSV será modificado com backup .bak: {args.csv}')
        folder=execute(args.command,args.folder if args.command=='merge' else args.csv,
                       args.output,recursive=getattr(args,'recursive',False),
                       self_loops=getattr(args,'self_loops',False),mode=getattr(args,'mode','static'),timezone_name=getattr(args,'timezone','America/Sao_Paulo'),
                       cleaning_mode=getattr(args,'cleaning_mode','post'),
                       ranking_size=getattr(args,'ranking_size','15'),exclude_tag=getattr(args,'exclude_tag',None))
        print(f'Resultados em: {folder}')
    except Exception as exc:
        p.exit(1,f'Erro: {exc}\n')


if __name__=='__main__':
    main()
