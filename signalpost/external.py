"""Independent news is published only after a fresh article proves its subject."""
import re
from datetime import datetime,timezone
from .web import Page,words,stated_numbers,parse_date,_types


def independent_article(profile,response,record):
    page=Page(response);today=datetime.now(timezone.utc).date();text=page.root.text()
    legal=words(record.get('navn'));got=words(text)
    if not legal or not any(got[i:i+len(legal)]==legal for i in range(len(got)-len(legal)+1)):return
    address=record.get('forretningsadresse') or {};street=address.get('adresse') or []
    street=' '.join(street) if isinstance(street,list) else str(street)
    location=bool(street and address.get('postnummer') and str(address['postnummer']) in text and ' '.join(words(street)) in ' '.join(got))
    exact=str(record['organisasjonsnummer']) in stated_numbers(text)
    if not exact and not location:return
    for item,span in page.structured():
        if not _types(item)&{'NewsArticle','Article','BlogPosting'}:continue
        title=item.get('headline') or item.get('name');published=parse_date(item.get('datePublished'))
        if not title or not published or not 0<=(today-published).days<=730:continue
        if re.search(r'hello world|lorem ipsum|casino',str(title),re.I):continue
        profile.add('dated_activity',{'title':str(title),'date':published.isoformat(),'url':response.url,
                    'publisher_type':'independent_reporting','subject_basis':'full legal name and '+('exact organisation number' if exact else 'registered street and postcode')},
                    response,page.raw,'permitted_public_publisher',period=published.isoformat(),confidence=.98,method='independent_article_subject_gate')
        return
