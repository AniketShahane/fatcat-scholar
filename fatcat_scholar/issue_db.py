
import sys
import json
import sqlite3
import argparse
from dataclasses import dataclass
from typing import List, Dict, Tuple, Optional, Any, Sequence
import fatcat_openapi_client
import elasticsearch
from elasticsearch_dsl import Search, Q

@dataclass
class SimPubRow:
    sim_pubid: str
    pub_collection: str
    title: str
    issn: Optional[str]
    pub_type: Optional[str]
    publisher: Optional[str]

    container_issnl: Optional[str]
    container_ident: Optional[str]
    wikidata_qid: Optional[str]

    def tuple(self):
        return (self.sim_pubid, self.pub_collection, self.title, self.issn, self.pub_type, self.publisher, self.container_issnl, self.container_ident, self.wikidata_qid)

@dataclass
class SimIssueRow:
    """
    TODO:
    - distinguish between release count that can do full link with pages, or just in this year/volume/issue?
    """
    issue_item: str
    sim_pubid: str
    year: Optional[int]
    volume: Optional[str]
    issue: Optional[str]
    first_page: Optional[int]
    last_page: Optional[int]
    release_count: Optional[int]

    def tuple(self):
        return (self.issue_item, self.sim_pubid, self.year, self.volume, self.issue, self.first_page, self.last_page, self.release_count)

@dataclass
class ReleaseCountsRow:
    sim_pubid: str
    year_in_sim: bool
    release_count: int
    year: Optional[int]
    volume: Optional[str]

    def tuple(self):
        return (self.sim_pubid, self.year, self.volume, self.year_in_sim, self.release_count)


def es_issue_count(es_client: Any, container_id: str, year: int, volume: str, issue: str) -> int:
    search = Search(using=es_client, index="fatcat_release")
    search = search\
        .filter("term", container_id=container_id)\
        .filter("term", year=year)\
        .filter("term", volume=volume)\
        .filter("term", issue=issue)

    return search.count()

def es_container_aggs(es_client: Any, container_id: str) -> List[Dict[str, Any]]:
    """
    """
    query = {
        "size": 0,
        "query": {
            "term": { "container_id": ident }
        },
        "aggs": { "container_stats": { "filters": { "filters": {
                  "in_web": { "term": { "in_web": "true" } },
                  "in_kbart": { "term": { "in_kbart": "true" } },
                  "is_preserved": { "term": { "is_preserved": "true" } },
        }}}}
    }
    params=dict(request_cache="true")
    buckets = resp['aggregations']['container_stats']['buckets']
    stats = {
        'ident': ident,
        'issnl': issnl,
        'total': resp['hits']['total'],
        'in_web': buckets['in_web']['doc_count'],
        'in_kbart': buckets['in_kbart']['doc_count'],
        'is_preserved': buckets['is_preserved']['doc_count'],
    }
    return stats

class IssueDB():

    def __init__(self, db_file):
        """
        To create a temporary database, pass ":memory:" as db_file
        """
        self.db = sqlite3.connect(db_file, isolation_level='EXCLUSIVE')
        self._pubid2container_map: Dict[str, Optional[str]] = dict()

    def init_db(self):
        self.db.executescript("""
            PRAGMA main.page_size = 4096;
            PRAGMA main.cache_size = 20000;
            PRAGMA main.locking_mode = EXCLUSIVE;
            PRAGMA main.synchronous = OFF;
        """)
        with open('schema/issue_db.sql', 'r') as fschema:
            self.db.executescript(fschema.read())

    def insert_sim_pub(self, pub: SimPubRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        cur.execute("INSERT OR REPLACE INTO sim_pub VALUES (?,?,?,?,?,?,?,?,?)",
            pub.tuple())

    def insert_sim_issue(self, issue: SimIssueRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        cur.execute("INSERT OR REPLACE INTO sim_issue VALUES (?,?,?,?,?,?,?,?)",
            issue.tuple())

    def insert_release_counts(self, counts: ReleaseCountsRow, cur: Any = None) -> None:
        if not cur:
            cur = self.db.cursor()
        cur.execute("INSERT OR REPLACE INTO release_counts VALUES (?,?,?,?,?,?,?,?,?)",
            counts.tuple())

    def pubid2container(self, sim_pubid: str) -> Optional[str]:
        if sim_pubid in self._pubid2container_map:
            return self._pubid2container_map[sim_pubid]
        row = list(self.db.execute("SELECT container_ident FROM sim_pub WHERE sim_pubid = ?;", [sim_pubid]))
        if row:
            self._pubid2container_map[sim_pubid] = row[0][0]
            return row[0][0]
        else:
            self._pubid2container_map[sim_pubid] = None
            return None

    def load_pubs(self, json_lines: Sequence[str], api: Any):
        """
        Reads a file (or some other iterator) of JSON lines, parses them into a
        dict, then inserts rows.
        """
        cur = self.db.cursor()
        for line in json_lines:
            if not line:
                continue
            obj = json.loads(line)
            meta = obj['metadata']
            assert "periodicals" in meta['collection']
            container: Optional[ContainerEntity] = None
            if meta.get('issn'):
                try:
                    container = api.lookup_container(issnl=meta['issn'])
                except fatcat_openapi_client.ApiException as ae:
                    if ae.status != 404:
                        raise ae
            row = SimPubRow(
                sim_pubid=meta['sim_pubid'],
                pub_collection=meta['identifier'],
                title=meta['title'],
                issn=meta.get('issn'),
                pub_type=meta.get('pub_type'),
                publisher=meta.get('publisher'),
                container_issnl=container and container.issnl,
                container_ident=container and container.ident,
                wikidata_qid=container and container.wikidata_qid,
            )
            self.insert_sim_pub(row, cur)
        cur.close()
        self.db.commit()

    def load_issues(self, json_lines: Sequence[str], es_client: Any):
        """
        Reads a file (or some other iterator) of JSON lines, parses them into a
        dict, then inserts rows.
        """
        cur = self.db.cursor()
        for line in json_lines:
            if not line:
                continue
            obj = json.loads(line)
            meta = obj['metadata']
            assert "periodicals" in meta['collection']
            #pub_collection = [c for c in meta['collection'] if c.startswith("pub_")][0]
            issue_item = meta['identifier']

            # don't index meta items
            # TODO: handle more weird suffixes like "1-2", "_part_1", "_index-contents"
            if issue_item.endswith("_index") or issue_item.endswith("_contents"):
                continue

            sim_pubid=meta['sim_pubid']

            year: Optional[int] = None
            if meta.get('date'):
                year = int(meta['date'][:4])
            volume = meta.get('volume')
            issue = meta.get('issue')

            first_page: Optional[int] = None
            last_page: Optional[int] = None
            if obj.get('page_numbers'):
                pages = [p['pageNumber'] for p in obj['page_numbers']['pages'] if p['pageNumber']]
                pages = [int(p) for p in pages if p.isdigit()]
                if len(pages):
                    first_page = min(pages)
                    last_page = max(pages)

            release_count: Optional[int] = None
            if year and volume and issue:
                container_id = self.pubid2container(sim_pubid)
                if container_id:
                    release_count = es_issue_count(es_client, container_id, year, volume, issue)

            row = SimIssueRow(
                issue_item=issue_item,
                sim_pubid=sim_pubid,
                year=year,
                volume=volume,
                issue=issue,
                first_page=first_page,
                last_page=last_page,
                release_count=release_count,
            )
            self.insert_sim_issue(row, cur)
        cur.close()
        self.db.commit()


def main():
    """
    Run this command like:

        python -m fatcat_scholar.issue_db
    """

    parser = argparse.ArgumentParser(
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    subparsers = parser.add_subparsers()

    parser.add_argument("--db-file",
        help="sqlite3 database file to open",
        default='issue_db.sqlite',
        type=str)

    sub = subparsers.add_parser('init_db',
        help="create sqlite3 output file and tables")
    sub.set_defaults(func='init_db')

    sub = subparsers.add_parser('load_pubs',
        help="update container-level stats from JSON file")
    sub.set_defaults(func='load_pubs')
    sub.add_argument("json_file",
        help="collection-level metadata, as JSON-lines",
        nargs='?', default=sys.stdin, type=argparse.FileType('r'))

    sub = subparsers.add_parser('load_issues',
        help="update item-level stats from JSON file")
    sub.set_defaults(func='load_issues')
    sub.add_argument("json_file",
        help="item-level metadata, as JSON-lines",
        nargs='?', default=sys.stdin, type=argparse.FileType('r'))

    args = parser.parse_args()
    if not args.__dict__.get("func"):
        print("tell me what to do! (try --help)")
        sys.exit(-1)

    idb = IssueDB(args.db_file)
    api = fatcat_openapi_client.DefaultApi(fatcat_openapi_client.ApiClient())
    es_client = elasticsearch.Elasticsearch("https://search.fatcat.wiki")

    if args.func == 'load_pubs':
        idb.load_pubs(args.json_file, api)
    elif args.func == 'load_issues':
        idb.load_issues(args.json_file, es_client)
    else:
        func = getattr(idb, args.func)
        func()

if __name__=="__main__":
    main()