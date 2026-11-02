"""Page claims before collecting reviews on deep OFFSET pages.

The original review predicate and aggregate ordering are retained verbatim. All
page filters and parameters stay inside the materialized page. Unsupported SQL
shapes use the original query. No review or claim data is logged.
"""
from functools import lru_cache
import re


def _tokens(sql):
    result=[];pairs={};stack=[];i=0;depth=0
    while i<len(sql):
        c=sql[i]
        if c.isspace():i+=1;continue
        if sql.startswith('--',i):
            end=sql.find('\n',i);i=len(sql) if end<0 else end+1;continue
        if sql.startswith('/*',i):
            end=sql.find('*/',i+2)
            if end<0:raise ValueError('SQL_COMMENT_INVALID')
            i=end+2;continue
        start=i
        if c in "'\"":
            quote=c;i+=1
            while i<len(sql):
                if sql[i]=='\\':i+=2;continue
                if sql[i]==quote:
                    if i+1<len(sql) and sql[i+1]==quote:i+=2;continue
                    i+=1;break
                i+=1
            else:raise ValueError('SQL_QUOTE_INVALID')
            result.append((sql[start:i],'string' if quote=="'" else 'quoted',start,i,depth));continue
        if c=='(':
            result.append((c,'punct',i,i+1,depth));stack.append(i);depth+=1;i+=1;continue
        if c==')':
            if not stack:raise ValueError('SQL_PARENS_INVALID')
            depth-=1;pairs[stack.pop()]=i+1;result.append((c,'punct',i,i+1,depth));i+=1;continue
        match=re.match(r'[A-Za-z_][A-Za-z0-9_$]*',sql[i:])
        if match:
            i+=len(match.group());result.append((sql[start:i],'word',start,i,depth));continue
        result.append((c,'punct',i,i+1,depth));i+=1
    if stack:raise ValueError('SQL_PARENS_INVALID')
    return result,pairs


def _references(expression):
    tokens,_=_tokens(expression)
    return {tokens[i][0] for i in range(len(tokens)-2)
            if tokens[i][1]=='word' and tokens[i+1][0]=='.' and tokens[i+2][1]=='word'}


@lru_cache(maxsize=64)
def rewrite_review_query(sql):
    if not isinstance(sql,str) or '__litkg_review_page' in sql:return None
    if 'review_items' not in sql or 'jsonb_agg' not in sql:return None
    try:
        tokens,pairs=_tokens(sql)
        main_from=next(i for i,t in enumerate(tokens) if t[0].lower()=='from' and t[4]==0 and tokens[i+1][0].lower()=='claims')
        alias_token=main_from+2
        if tokens[alias_token][0].lower()=='as':alias_token+=1
        claim_alias=tokens[alias_token][0]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',claim_alias):return None
        review_from=next(i for i,t in enumerate(tokens) if t[0].lower()=='from' and tokens[i+1][0].lower()=='review_items')
        from_pos=tokens[review_from][2]
        spans=[]
        for start,end in pairs.items():
            if start<from_pos<end:
                inside=[t for t in tokens if start<t[2]<end-1]
                if inside and inside[0][0].lower()=='select':spans.append((start,end))
        if len(spans)!=1:return None
        start,end=spans[0];level=tokens[review_from][4]
        ri=review_from+2
        if tokens[ri][0].lower()=='as':ri+=1
        if tokens[ri][0].lower()=='where':review_alias='review_items'
        else:review_alias=tokens[ri][0]
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*',review_alias):return None
        where=next(t for t in tokens[ri:] if t[0].lower()=='where' and t[4]==level and t[2]<end)
        predicate=sql[where[3]:end-1].strip()
        subquery=sql[start:end]
        if '%s' in subquery or not _references(predicate).issubset({claim_alias,review_alias}):return None
        if any(t[1]=='word' and t[0].lower() in {'select','group','union','limit','offset'} for t in _tokens(predicate)[0]):return None
        agg=next(i for i,t in enumerate(tokens) if start<t[2]<from_pos and t[0].lower()=='jsonb_agg')
        opening=tokens[agg+1]
        if opening[0]!='(':return None
        agg_end=pairs[opening[2]]
        order_index=next(i for i,t in enumerate(tokens) if opening[2]<t[2]<agg_end and t[4]==opening[4]+1 and t[0].lower()=='order' and tokens[i+1][0].lower()=='by')
        expression=sql[opening[3]:tokens[order_index][2]].strip()
        ordering=sql[tokens[order_index+1][3]:agg_end-1].strip()
        if expression.lower().startswith('distinct ') or not expression:return None
        if not _references(expression).issubset({review_alias}) or not _references(ordering).issubset({review_alias}):return None
        order_main=next(i for i,t in enumerate(tokens) if t[0].lower()=='order' and t[4]==0 and tokens[i+1][0].lower()=='by')
        limit_main=next(i for i,t in enumerate(tokens) if t[0].lower()=='limit' and t[4]==0)
        page_order=sql[tokens[order_main+1][3]:tokens[limit_main][2]].strip()
        if not re.fullmatch(re.escape(claim_alias)+r'\s*\.\s*id(?:\s+ASC)?',page_order,re.I):return None
        page=(sql[:start]+"'[]'::jsonb"+sql[end:]).strip().rstrip(';')
        optimized=f'''WITH __litkg_review_page AS MATERIALIZED (
{page}
)
SELECT {claim_alias}.*,
       COALESCE({subquery}, '[]'::jsonb) AS __litkg_reviews_payload
FROM __litkg_review_page {claim_alias}
ORDER BY {claim_alias}.id'''
        return optimized
    except (ValueError,IndexError,KeyError,StopIteration):return None


class ReviewBatchCursor:
    def __init__(self,cursor):
        self.cursor=cursor;self.optimized=False;self._remaining=None

    def __getattr__(self,name):return getattr(self.cursor,name)

    def execute(self,query,params=None):
        # Keep the indexed first-page/keyset path unchanged. Only skipped rows
        # on OFFSET pages need protection from expensive review aggregation.
        offset=params[-1] if isinstance(params,(tuple,list)) and params else None
        offset_shape=isinstance(query,str) and re.search(r'LIMIT\s+%s\s+OFFSET\s+%s\s*;?\s*$',query,re.I)
        rewritten=rewrite_review_query(query) if type(offset) is int and offset>0 and offset_shape else None
        self.optimized=rewritten is not None;self._remaining=None
        return self.cursor.execute(rewritten if rewritten is not None else query,params)

    def fetchall(self):
        if self._remaining is not None:
            result=self._remaining;self._remaining=[];return result
        rows=self.cursor.fetchall()
        if not self.optimized or not rows:return rows
        for row in rows:
            value=row.pop('__litkg_reviews_payload')
            if not isinstance(value,list):raise ValueError('REVIEW_PAGE_PAYLOAD_INVALID')
            row['reviews']=value
        return rows

    def fetchone(self):
        if not self.optimized:return self.cursor.fetchone()
        if self._remaining is None:self._remaining=self.fetchall()
        return self._remaining.pop(0) if self._remaining else None
