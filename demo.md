# markdown-review demo

Bu dosya markdown-review'un temel özelliklerini tek yerde göstermek için hazırlandı.

## 1. Blok bazlı review

Her paragraf, başlık, liste elemanı, kod bloğu ve tablo kendi review item kimliğini alır. Bu paragrafın üzerinde hem yorum hem highlight örneği var.

- İlk liste elemanı ayrı bir review item olarak yorumlanabilir.
- İkinci liste elemanı da ayrı bir review item kimliği taşır.

## 2. Highlight örnekleri

Bu paragrafta özellikle **seçili kelimeler** ve `inline code` üzerinde highlight notları gösterilebilir.

## 3. Öneri akışı

Bu paragraf biraz zayıf yazıldı. Review sırasında replace önerisiyle daha net hale getirilebilir.

Bu paragrafın arkasına yeni bir paragraf ekleme önerisi var.

Bu yeni paragraf, kabul edildiğinde iki paragraf arasına otomatik boşlukla eklenecek.


## 4. Kod ve tablo

```python
def normalize(text: str) -> str:
    return " ".join(text.split())
```

| Özellik | Durum |
|---|---|
| Annotation | Var |
| Highlight | Var |
| Suggestion | Var |

## 5. Matematik

Basit bir inline formül: $a^2 + b^2 = c^2$. Bu satır accepted suggestion ve version history örneği olarak güncellendi.

Bu paragraf dışarıdan değiştirildiği için eski yorum orphan paneline taşınır.
