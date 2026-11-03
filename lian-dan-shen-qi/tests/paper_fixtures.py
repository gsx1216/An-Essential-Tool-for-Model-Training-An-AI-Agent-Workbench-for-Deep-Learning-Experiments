"""内存中的可重复 PDF 测试样本，不创建对外交付的文档。"""
import io
import textwrap


def pdf_bytes(pages,*,encrypted=False):
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject,NameObject,DecodedStreamObject
    writer=PdfWriter()
    font=DictionaryObject({NameObject('/Type'):NameObject('/Font'),NameObject('/Subtype'):NameObject('/Type1'),NameObject('/BaseFont'):NameObject('/Helvetica')})
    font=writer._add_object(font)
    for text in pages:
        page=writer.add_blank_page(width=612,height=792)
        page[NameObject('/Resources')]=DictionaryObject({NameObject('/Font'):DictionaryObject({NameObject('/F1'):font})})
        lines=['BT /F1 10 Tf 12 TL 40 740 Td']
        for line in textwrap.wrap(text,80):
            line=line.replace('\\','\\\\').replace('(','\\(').replace(')','\\)')
            lines.append('('+line+') Tj T*')
        lines.append('ET');stream=DecodedStreamObject();stream.set_data('\n'.join(lines).encode('latin-1'))
        page[NameObject('/Contents')]=writer._add_object(stream)
    if encrypted:writer.encrypt('TEST_ONLY_PDF_PASSWORD')
    out=io.BytesIO();writer.write(out);return out.getvalue()


PAPER_PAGES=[
    'Camouflaged object detection uses boundary supervision. The training loss combines prediction loss and boundary loss. MAE is an evaluation metric.',
    'An ablation study should keep the dataset and evaluation protocol fixed. Lower mean absolute error is better for the same test split.'
]
