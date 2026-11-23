#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Matchers for law.go.kr.

The web/CLI pipeline enables current_names_only: current exact names, no history
or fuzzy substitution. Public ELIS fallback lives in retrieval_policy.py.
The following legacy ranking helpers remain available for diagnostics/tests:

1) exact/current
2) normalized exact / local-title fingerprint
3) history -> current identity bridge
4) same-municipality catalog + robust title similarity
5) requested article existence validation
6) ambiguous or source-missing cases remain reviewable instead of being fabricated.
"""
from __future__ import annotations
import requests
from functools import lru_cache

import copy, math, re
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable

import lawgo_core as core

GENERIC = {
    "관한","등에","및","위한","운영","관리","기준","규정","조례","규칙","훈령","예규","고시",
    "사무","처리","시행","일부","지정","업무","지원","설치","징수","부과","사업","관리운영"
}
TYPE_WORDS = ("조례","규칙","훈령","예규","고시","공고","규정","지침","내규")
PROVINCE_ALIASES = {
    "강원":{"강원","강원도","강원특별자치도"}, "전북":{"전북","전라북도","전북특별자치도"},
    "제주":{"제주","제주도","제주특별자치도"}, "경기":{"경기","경기도"}, "충북":{"충북","충청북도"},
    "충남":{"충남","충청남도"}, "전남":{"전남","전라남도"}, "경북":{"경북","경상북도"}, "경남":{"경남","경상남도"},
    "서울":{"서울","서울특별시"}, "부산":{"부산","부산광역시"}, "대구":{"대구","대구광역시"},
    "인천":{"인천","인천광역시"}, "광주":{"광주","광주광역시"}, "대전":{"대전","대전광역시"},
    "울산":{"울산","울산광역시"}, "세종":{"세종","세종특별자치시"},
}
for k, vals in list(PROVINCE_ALIASES.items()):
    for v in list(vals): PROVINCE_ALIASES.setdefault(v, vals)
AMBIG_LOCAL={"고성군","중구","동구","서구","남구","북구"}


def clean_title(v: Any) -> str:
    s=core.norm_space(v)
    s=s.replace("ㆍ","·").replace("․","·")
    s=re.sub(r"[‘’'\"“”「」『』《》〈〉()\[\]{}]", "", s)
    s=re.sub(r"[·,;:!?/\\|.\-]+", "", s)
    return re.sub(r"\s+", "", s)


def municipality_parts(v: str) -> tuple[str,str]:
    toks=[x for x in core.norm_space(v).split() if x]
    return (toks[0] if len(toks)>=2 else "", toks[-1] if toks else "")


def municipality_match(source: str, api_name: str) -> bool:
    s,a=core.norm_space(source),core.norm_space(api_name)
    if not s or not a: return True
    prov,local=municipality_parts(s)
    an=core.norm_name(a)
    if local and core.norm_name(local) not in an: return False
    if local in AMBIG_LOCAL and prov:
        aliases=PROVINCE_ALIASES.get(prov,{prov})
        has_any_prov=any(core.norm_name(x) in an for vals in PROVINCE_ALIASES.values() for x in vals)
        if has_any_prov and not any(core.norm_name(x) in an for x in aliases): return False
    return True


def strip_local_prefix(title: str, municipality: str) -> str:
    s=core.norm_space(title); _,local=municipality_parts(municipality)
    variants=[local]
    if local.endswith(("시","군","구")): variants.append(local[:-1])
    for p in sorted({x for x in variants if x},key=len,reverse=True):
        s=re.sub(rf"^\s*{re.escape(p)}\s*", "", s)
    return s.strip()


def strip_type_suffix(title: str) -> str:
    s=core.norm_space(title)
    for t in TYPE_WORDS:
        s=re.sub(rf"\s*{re.escape(t)}\s*$", "", s)
    return s.strip()


def fingerprint(title: str, municipality: str="") -> str:
    s=strip_local_prefix(title,municipality)
    s=strip_type_suffix(s)
    # 법규명에서 의미가 약한 조사/연결 표현은 fingerprint에만 제거한다.
    s=re.sub(r"(?:에\s*관한|에\s*대한|등에\s*관한|을\s*위한|를\s*위한)", "", s)
    return clean_title(s)


def hangul_jamo(s: str) -> str:
    cho="ㄱㄲㄴㄷㄸㄹㅁㅂㅃㅅㅆㅇㅈㅉㅊㅋㅌㅍㅎ"; jung="ㅏㅐㅑㅒㅓㅔㅕㅖㅗㅘㅙㅚㅛㅜㅝㅞㅟㅠㅡㅢㅣ"; jong=" ㄱㄲㄳㄴㄵㄶㄷㄹㄺㄻㄼㄽㄾㄿㅀㅁㅂㅄㅅㅆㅇㅈㅊㅋㅌㅍㅎ"
    out=[]
    for ch in clean_title(s):
        o=ord(ch)
        if 0xAC00<=o<=0xD7A3:
            n=o-0xAC00; out += [cho[n//588],jung[(n%588)//28]]
            j=n%28
            if j: out.append(jong[j])
        else: out.append(ch)
    return "".join(out)


def grams(s:str,n:int)->Counter:
    s=clean_title(s)
    if not s:return Counter()
    if len(s)<n:return Counter({s:1})
    return Counter(s[i:i+n] for i in range(len(s)-n+1))


def cosine(a:Counter,b:Counter)->float:
    if not a or not b:return 0.0
    dot=sum(v*b.get(k,0) for k,v in a.items()); na=math.sqrt(sum(v*v for v in a.values())); nb=math.sqrt(sum(v*v for v in b.values()))
    return dot/(na*nb) if na and nb else 0.0


def tokens(v:str,municipality:str="")->set[str]:
    s=strip_type_suffix(strip_local_prefix(core.norm_space(v),municipality))
    ts=set(re.findall(r"[가-힣A-Za-z0-9]+",s))
    return {x for x in ts if len(x)>=2 and x not in GENERIC}


def token_overlap(a:str,b:str,municipality:str="")->float:
    aa,bb=tokens(a,municipality),tokens(b,municipality)
    if not aa or not bb:return 0.0
    return len(aa&bb)/max(1,len(aa|bb))


@lru_cache(maxsize=20000)
def similarity(a:str,b:str,municipality:str="")->dict:
    aa,bb=clean_title(a),clean_title(b)
    if not aa or not bb:return {"score":0.0,"token":0.0,"fingerprint":0.0}
    if aa==bb:return {"score":1.0,"token":1.0,"fingerprint":1.0}
    fa,fb=fingerprint(a,municipality),fingerprint(b,municipality)
    fp=SequenceMatcher(None,fa,fb).ratio() if fa and fb else 0.0
    seq=SequenceMatcher(None,aa,bb).ratio(); jam=SequenceMatcher(None,hangul_jamo(aa),hangul_jamo(bb)).ratio()
    bi=cosine(grams(aa,2),grams(bb,2)); tri=cosine(grams(aa,3),grams(bb,3)); tok=token_overlap(a,b,municipality)
    # fingerprint와 token을 이전보다 크게 반영: 띄어쓰기/법규종류/연결어 변동에 강함.
    score=.24*bi+.16*tri+.16*jam+.12*seq+.20*fp+.12*tok
    return {"score":min(1.0,max(0.0,score)),"token":tok,"fingerprint":fp}


def query_variants(title:str,municipality:str="")->list[str]:
    s=core.norm_space(title); local=strip_local_prefix(s,municipality); coretitle=strip_type_suffix(local)
    qs=[s,local,coretitle]
    # 연결어를 제거한 핵심 표현
    qs += [re.sub(r"(?:에 관한|에 대한|등에 관한|을 위한|를 위한)"," ",coretitle)]
    ts=[t for t in re.findall(r"[가-힣A-Za-z0-9]+",coretitle) if len(t)>=2 and t not in GENERIC]
    ts=sorted(ts,key=len,reverse=True)
    qs += ts[:5]
    compact=clean_title(coretitle)
    if len(compact)>=10:
        qs += [compact[:6],compact[-6:]]
    out=[];seen=set()
    for q in qs:
        q=core.norm_space(q)
        if q and q not in seen:seen.add(q);out.append(q)
    return out[:10]


class HighRecallLawApi(core.LawApi):
    def __init__(self,oc,cache_dir,delay=.10,timeout=30,auto_threshold=.88,review_threshold=.70,min_margin=.015,candidate_validation_limit=3):
        super().__init__(oc,cache_dir,delay,timeout)
        self.auto_threshold=auto_threshold;self.review_threshold=review_threshold;self.min_margin=min_margin
        self.candidate_validation_limit=max(1,int(candidate_validation_limit))
        self._ordin_catalog={};self._law_search={}

    def _law_candidates(self,title:str,nw:str="3")->list[dict]:
        key=(title,nw)
        if key in self._law_search:return copy.deepcopy(self._law_search[key])
        data=self._get(core.BASE_SEARCH,{"target":"eflaw","nw":nw,"search":"1","query":title,"display":"100","sort":"efdes"})
        rows=[d for d in core.iter_dicts(data) if "법령명한글" in d and "법령ID" in d]
        self._law_search[key]=rows
        return copy.deepcopy(rows)

    def _validate_law(self,meta:dict,target:dict|None)->bool|None:
        if not target or not target.get("article") or target.get("section")=="부칙":return None
        try:
            lid=core.first_scalar(meta,("법령ID","LID")); return core.extract_law_subunit(self,lid,target).get("status")=="OK"
        except requests.exceptions.RequestException:raise
        except Exception:return False

    def search_current_law_fuzzy(self,title:str,target_hint:dict|None=None)->tuple[dict|None,dict]:
        if getattr(self, "current_names_only", False):
            return self.search_current_exact(title)
        rows=self._law_candidates(title,"3")
        exact=[d for d in rows if core.norm_name(d.get("법령명한글"))==core.norm_name(title)]
        if exact:
            exact.sort(key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True);m=exact[0]
            return copy.deepcopy(m),{"method":"CURRENT_EXACT","score":1.0,"top":m.get("법령명한글"),"second":None,"margin":1.0,"auto":True,"article_valid":self._validate_law(m,target_hint),"candidate_pool":len(rows)}
        # history exact -> LID current bridge
        hist=self._law_candidates(title,"1,2,3")
        he=[d for d in hist if core.norm_name(d.get("법령명한글"))==core.norm_name(title)]
        if he:
            he.sort(key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True);lid=core.first_scalar(he[0],("법령ID","LID"))
            cur=self._get(core.BASE_SEARCH,{"target":"eflaw","nw":"3","LID":str(lid),"display":"100","sort":"efdes"})
            cc=[d for d in core.iter_dicts(cur) if "법령명한글" in d and "법령ID" in d]
            if cc:
                cc.sort(key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True);m=cc[0]
                return copy.deepcopy(m),{"method":"CURRENT_BY_LID_FROM_HISTORY","score":1.0,"top":m.get("법령명한글"),"second":None,"margin":1.0,"auto":True,"article_valid":self._validate_law(m,target_hint),"candidate_pool":len(cc),"historical_title":title}
            return None,{"method":"HISTORY_FOUND_NO_CURRENT","score":1.0,"top":title,"auto":False,"candidate_pool":0}
        # fuzzy over search candidates from several query variants
        pool={}
        for q in query_variants(title)[:6]:
            for d in self._law_candidates(q,"3"):
                pool[str(core.first_scalar(d,("법령ID","LID")) or core.norm_name(d.get("법령명한글")))]=d
        ranked=[]
        for d in pool.values():
            sm=similarity(title,core.norm_space(d.get("법령명한글")))
            ranked.append([sm["score"],d,None,sm])
        ranked.sort(key=lambda x:x[0],reverse=True)
        for r in ranked[:self.candidate_validation_limit]:
            r[2]=self._validate_law(r[1],target_hint)
            if r[2] is True:r[0]=min(1,r[0]+.04)
            elif r[2] is False:r[0]=max(0,r[0]-.08)
        ranked.sort(key=lambda x:x[0],reverse=True)
        if not ranked:return None,{"method":"NOT_FOUND","score":0.0,"auto":False,"candidate_pool":0}
        top=ranked[0];sec=ranked[1] if len(ranked)>1 else [0,None,None,{}];margin=top[0]-sec[0]
        info={"method":"FUZZY_LAW_REVIEW","score":round(top[0],6),"top":top[1].get("법령명한글"),"second":sec[1].get("법령명한글") if sec[1] else None,"second_score":round(sec[0],6),"margin":round(margin,6),"article_valid":top[2],"auto":False,"candidate_pool":len(ranked)}
        if top[0]>=.93 and margin>=.03 and top[2] is not False:
            info["method"]="FUZZY_LAW_AUTO";info["auto"]=True;return copy.deepcopy(top[1]),info
        return None,info

    def _paged_ordin(self,query:str,nw:int=1,max_pages:int=80)->list[dict]:
        out=[];page=1
        while page<=max_pages:
            data=self._get(core.BASE_SEARCH,{"target":"ordin","nw":str(nw),"search":"1","query":query,"display":"100","page":str(page),"sort":"efdes"})
            root=next((d for d in core.iter_dicts(data) if "totalCnt" in d and "target" in d),{})
            rows=[d for d in core.iter_dicts(data) if "자치법규명" in d and ("자치법규ID" in d or "자치법규일련번호" in d)]
            out.extend(rows)
            total=int(str(root.get("totalCnt") or len(out)).replace(",","")) if root else len(out)
            if not rows or len(out)>=total:break
            page+=1
        # dedup
        by={}
        for d in out:
            k=str(core.first_scalar(d,("자치법규ID","ID")) or core.first_scalar(d,("자치법규일련번호","MST")) or (core.norm_name(d.get("지자체기관명")),core.norm_name(d.get("자치법규명"))))
            if k not in by or core.safe_int_date(d.get("시행일자"))>core.safe_int_date(by[k].get("시행일자")):by[k]=d
        return list(by.values())

    def current_ordinance_catalog(self,municipality:str)->list[dict]:
        if municipality in self._ordin_catalog:return copy.deepcopy(self._ordin_catalog[municipality])
        _,local=municipality_parts(municipality)
        rows=self._paged_ordin(local or municipality,1)
        rows=[d for d in rows if municipality_match(municipality,d.get("지자체기관명",""))]
        self._ordin_catalog[municipality]=rows
        return copy.deepcopy(rows)

    def _validate_ordin(self,meta:dict,target:dict|None)->bool|None:
        if not target or not target.get("article") or target.get("section")=="부칙":return None
        try:
            oid=core.first_scalar(meta,("자치법규ID","ID"));mst=core.first_scalar(meta,("자치법규일련번호","MST"))
            body=self.current_ordinance_body(oid) if oid else self._get(core.BASE_SERVICE,{"target":"ordin","MST":str(mst)})
            return core.extract_ordin_article(body,target).get("status")=="OK"
        except requests.exceptions.RequestException:raise
        except Exception:return False

    def _rank_ordin(self,title:str,municipality:str,slot:str,cands:list[dict],target:dict|None)->tuple[dict|None,dict]:
        ranked=[]
        for d in cands:
            name=core.norm_space(d.get("자치법규명")); sm=similarity(title,name,municipality)
            kind_ok=core.ordinance_type_match(slot,d.get("자치법규종류")); score=sm["score"] + (.02 if kind_ok else 0)
            ranked.append([min(1,score),d,None,sm,kind_ok])
        ranked.sort(key=lambda x:x[0],reverse=True)
        for r in ranked[:self.candidate_validation_limit]:
            r[2]=self._validate_ordin(r[1],target)
            if r[2] is True:r[0]=min(1,r[0]+.055)
            elif r[2] is False:r[0]=max(0,r[0]-.10)
        ranked.sort(key=lambda x:x[0],reverse=True)
        if not ranked:return None,{"method":"NOT_FOUND","score":0.0,"auto":False,"candidate_pool":0,"region":{"method":"NAME_CATALOG_AND_RULES"}}
        t=ranked[0];s=ranked[1] if len(ranked)>1 else [0,None,None,{},False];margin=t[0]-s[0]
        info={"method":"FUZZY_REVIEW_REQUIRED","score":round(t[0],6),"top":t[1].get("자치법규명"),"second":s[1].get("자치법규명") if s[1] else None,"second_score":round(s[0],6),"margin":round(margin,6),"article_valid":t[2],"auto":False,"candidate_pool":len(ranked),"type_mismatch":not t[4],"token_overlap":round(t[3]["token"],6),"fingerprint_score":round(t[3]["fingerprint"],6),"region":{"method":"NAME_CATALOG_AND_RULES"}}
        # explainable auto rules
        exact_fp=t[3]["fingerprint"]>=.985 and t[2] is not False
        strong=t[0]>=self.auto_threshold and margin>=self.min_margin and t[2] is not False
        article_backed=t[2] is True and t[0]>=.74 and t[3]["fingerprint"]>=.70 and margin>=.015
        unique_article=t[2] is True and t[0]>=.68 and t[3]["token"]>=.55 and margin>=.05
        if exact_fp or strong or article_backed or unique_article:
            info["method"]="RULE_AUTO_EXACT_FINGERPRINT" if exact_fp else ("FUZZY_AUTO_STRONG" if strong else "FUZZY_AUTO_ARTICLE_VALIDATED")
            info["auto"]=True;return copy.deepcopy(t[1]),info
        if t[0]<self.review_threshold:info["method"]="FUZZY_LOW_CONFIDENCE"
        return None,info

    def search_current_ordinance_fuzzy(self,title:str,municipality:str,slot:str,target_hint:dict|None=None)->tuple[dict|None,dict]:
        if getattr(self, "current_names_only", False):
            return self.search_current_exact(title, municipality, slot)
        catalog=self.current_ordinance_catalog(municipality)
        # 1. exact normalized, type irrelevant if exact name + local municipality
        exact=[d for d in catalog if core.norm_name(d.get("자치법규명"))==core.norm_name(title)]
        if exact:
            exact.sort(key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True);m=exact[0]
            kind_ok=core.ordinance_type_match(slot,m.get("자치법규종류"))
            return copy.deepcopy(m),{"method":"CURRENT_EXACT_LOCAL" if kind_ok else "CURRENT_EXACT_LOCAL_TYPE_MISMATCH","score":1.0 if kind_ok else .99,"top":m.get("자치법규명"),"second":None,"margin":1.0,"auto":True,"article_valid":self._validate_ordin(m,target_hint),"candidate_pool":len(catalog),"type_mismatch":not kind_ok,"region":{"method":"NAME_CATALOG_AND_RULES"}}
        # 2. fingerprint exact: handles municipality prefix/type suffix/boilerplate/punctuation variation
        fp=fingerprint(title,municipality)
        fpe=[d for d in catalog if fp and fingerprint(d.get("자치법규명",""),municipality)==fp]
        if len(fpe)==1:
            m=fpe[0];return copy.deepcopy(m),{"method":"CURRENT_RULE_FINGERPRINT_EXACT","score":.995,"top":m.get("자치법규명"),"second":None,"margin":.995,"auto":True,"article_valid":self._validate_ordin(m,target_hint),"candidate_pool":len(catalog),"type_mismatch":not core.ordinance_type_match(slot,m.get("자치법규종류")),"region":{"method":"NAME_CATALOG_AND_RULES"}}
        # 3. targeted queries can find records omitted from broad local-name catalog
        merged={str(core.first_scalar(d,("자치법규ID","ID")) or core.first_scalar(d,("자치법규일련번호","MST")) or core.norm_name(d.get("자치법규명"))):d for d in catalog}
        for q in query_variants(title,municipality):
            for d in self._paged_ordin(q,1,max_pages=30):
                if municipality_match(municipality,d.get("지자체기관명","")):
                    k=str(core.first_scalar(d,("자치법규ID","ID")) or core.first_scalar(d,("자치법규일련번호","MST")) or core.norm_name(d.get("자치법규명")));merged[k]=d
            # A full-title search already includes the exact current title candidates.
            # Do not issue broader keyword searches once that exact local match exists.
            if core.norm_name(q) == core.norm_name(title):
                exact = [d for d in merged.values() if core.norm_name(d.get("자치법규명")) == core.norm_name(title)]
                if exact:
                    break
        candidates=list(merged.values())
        exact=[d for d in candidates if core.norm_name(d.get("자치법규명"))==core.norm_name(title)]
        if exact:
            m=sorted(exact,key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True)[0]
            return copy.deepcopy(m),{"method":"CURRENT_EXACT_LOCAL_SEARCH","score":1.0,"top":m.get("자치법규명"),"second":None,"margin":1.0,"auto":True,"article_valid":self._validate_ordin(m,target_hint),"candidate_pool":len(candidates),"type_mismatch":not core.ordinance_type_match(slot,m.get("자치법규종류")),"region":{"method":"NAME_CATALOG_AND_RULES"}}
        # 4. historical exact -> current same ID
        hist=[]
        for q in query_variants(title,municipality)[:5]:
            hist.extend([d for d in self._paged_ordin(q,2,max_pages=30) if municipality_match(municipality,d.get("지자체기관명",""))])
        he=[d for d in hist if core.norm_name(d.get("자치법규명"))==core.norm_name(title)]
        if he:
            ids={str(core.first_scalar(d,("자치법규ID","ID")) or "") for d in he}
            same=[d for d in candidates if str(core.first_scalar(d,("자치법규ID","ID")) or "") in ids and str(core.first_scalar(d,("자치법규ID","ID")) or "")]
            if same:
                m=sorted(same,key=lambda d:(core.safe_int_date(d.get("시행일자")),core.safe_int_date(d.get("공포일자"))),reverse=True)[0]
                return copy.deepcopy(m),{"method":"CURRENT_BY_ID_FROM_ORDIN_HISTORY","score":1.0,"top":m.get("자치법규명"),"second":None,"margin":1.0,"auto":True,"article_valid":self._validate_ordin(m,target_hint),"candidate_pool":len(candidates),"historical_title":title,"region":{"method":"NAME_CATALOG_AND_RULES"}}
            return None,{"method":"ORDIN_HISTORY_FOUND_NO_CURRENT_ID","score":1.0,"top":title,"auto":False,"article_valid":False,"candidate_pool":len(candidates),"region":{"method":"NAME_CATALOG_AND_RULES"}}
        # 5. fuzzy local candidate ranking
        return self._rank_ordin(title,municipality,slot,candidates,target_hint)

    def search_current_exact(self, title, municipality=None, slot=None):
        """Current title only: no history bridge, fuzzy successor, or body validation."""
        if municipality is None:
            rows = self._law_candidates(title, "3")
            name_key = "법령명한글"
        else:
            rows = self.current_ordinance_catalog(municipality)
            name_key = "자치법규명"
            if not any(core.norm_name(d.get(name_key)) == core.norm_name(title) for d in rows):
                for query in query_variants(title, municipality)[:2]:
                    rows.extend(d for d in self._paged_ordin(query, 1, max_pages=10)
                                if municipality_match(municipality, d.get("지자체기관명", "")))
                    if any(core.norm_name(d.get(name_key)) == core.norm_name(title) for d in rows):
                        break
        exact = [d for d in rows if core.norm_name(d.get(name_key)) == core.norm_name(title)
                 and (slot is None or core.ordinance_type_match(slot, d.get("자치법규종류")))]
        if not exact:
            return None, {"method": "CURRENT_NAME_NOT_FOUND", "auto": False, "candidate_pool": len(rows)}
        exact.sort(key=lambda d: (core.safe_int_date(d.get("시행일자")), core.safe_int_date(d.get("공포일자"))), reverse=True)
        meta = copy.deepcopy(exact[0])
        return meta, {"method": "CURRENT_EXACT" if municipality is None else "CURRENT_EXACT_LOCAL", "score": 1.0,
                      "top": meta.get(name_key), "auto": True, "candidate_pool": len(rows)}

    def search_current_law(self, title):
        if getattr(self, "current_names_only", False):
            meta, info = self.search_current_exact(title)
            return meta, info["method"]
        return super().search_current_law(title)
