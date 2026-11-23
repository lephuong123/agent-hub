# Thư viện vendored (`static/vendor/`)

Phục vụ nguyên vẹn từ `/static/vendor/…` (CSP `'self'`, không CDN lúc chạy). Mỗi thư viện một thư mục `<tên>-<phiên bản>/`; đổi phiên bản =
thêm thư mục mới + sửa đường dẫn (với island Điều phối: `web/coord.py` `VENDOR`), không sửa tệp tại chỗ. Tệp được đóng gói vào wheel
(`packages = ["src/cfharness"]`, hatchling lấy mọi tệp trong gói).

## Island Điều phối (`/manager?ui=next`, `static/coord/*.js`) — nạp bằng import map

Tải ngày 2026-11-23 bằng `npm` tarball (kiểm `dist.integrity` sha512 của registry) và so trùng từng byte với
`https://cdn.jsdelivr.net/npm/<tên>@<phiên bản>/<tệp>`. Không sửa gì sau khi tải.

| Thư mục | Gói npm | Phiên bản | Giấy phép | Tệp (nguồn trong gói) | sha256 |
|---|---|---|---|---|---|
| `preact-11.1.1/` | `preact` | 11.1.1 | MIT | `preact.mjs` (`dist/preact.mjs`) | `37918605414d7dfb7540a77fc853cdc2bd108308ca506301bdefed32418b00e5` |
| | | | | `hooks.mjs` (`hooks/dist/hooks.mjs`) | `24231102dc3e509c9c657bca591524e348eb8a5272c9d40538faef9beeeb654c` |
| `htm-3.1.1/` | `htm` | 3.1.1 | Apache-2.0 | `htm.module.js` (`dist/htm.module.js`) | `ab33dd3f38059b9be4d5f5350128eefb2356639c4e0bbe9d9e8b3ba75847e9e4` |
| `preact-signals-core-1.14.5/` | `@preact/signals-core` | 1.14.5 | MIT | `signals-core.mjs` (`dist/signals-core.mjs`) | `0c80bc127d72ecb1496ef116ffddae75079829a3af01e7f417c8e5a6474c68b6` |
| `preact-signals-2.11.4/` | `@preact/signals` | 2.11.4 | MIT | `signals.mjs` (`dist/signals.mjs`) | `ba8177597f45acaca4d05dcf3966b527d0a8264cfa2b0a87ace92dff324217f8` |
| `streaming-markdown-0.2.15/` | `streaming-markdown` | 0.2.15 | MIT | `smd.min.js` (`smd.min.js`) | `20c0a39e6ec48b99e2f505f8488d2342fb3000d03523a7ced6cd94c14b15e8c5` |
| `dompurify-3.4.18/` | `dompurify` | 3.4.18 | MPL-2.0 OR Apache-2.0 | `purify.es.mjs` (`dist/purify.es.mjs`) | `1a06b721ad8b46d6174b21f22ea70290aaf3e5a81d65af3b7178113953f7c9b1` |

Integrity npm (sha512 của tarball):

- preact@11.1.1 `sha512-vpFVfai437pbuLri7XVeslCPfrEZX6Z1S2gV+MOFrA/kji+NKUXvH7y01EnVjA1guLigfkVp77Sqipd4SIllCg==`
- htm@3.1.1 `sha512-983Vyg8NwUE7JkZ6NmOqpCZ+sh1bKv2iYTlUkzlWmA5JD2acKoxd4KVxbMmxX/85mtfdnDmTFoNKcg5DGAvxNQ==`
- @preact/signals-core@1.14.5 `sha512-vcRv7Ityz5JVmeiVqIluLFzT/PDS/AlhHUpw3/aUWqcVUs6bDGIPXE/6RSC4evBNWYvnniozg5Syfj33al3gkg==`
- @preact/signals@2.11.4 `sha512-Rb+5RPo9gWSaHtzJeauhHxiC8KP/IF2DEPkHPEOGOnuy2wnJzRqyeLRankUCYd6ADqtxS6GnWGv6Ie343d8aYw==`
- streaming-markdown@0.2.15 `sha512-eSWjdvLSTznl2+FIzuxw1TRXwhjZiY1RfSygGCMpcNY8CrsaUM2zHtxe/JFcPoXCQxeO3TSJ72QMDIN5B/Ui1w==`
- dompurify@3.4.18 `sha512-ofe3wI/uNm68HInABYFJYqQxRw1EaKAjNdrFVKqjUY+UZurvNd2L/HuQe097s/4RivWiZyP6tT7aoT8atjfhwg==`

Phụ thuộc giữa các tệp (bare specifier → import map): `preact/hooks` → `preact`; `@preact/signals` → `preact`, `preact/hooks`,
`@preact/signals-core` (bản 2.11.4 cần `^1.14.5`). `htm`, `streaming-markdown`, `dompurify` không import gì.
DOMPurify chọn bản vá mới nhất của nhánh 3.4 (bộ lọc HTML — ưu tiên bản có sửa lỗi bảo mật mới nhất). Kiểm lại: `tests/test_coord_island.py`
(phiên bản trong import map, sha256 ở bảng trên khớp tệp, wheel có đủ tệp).

## Khác

| Thư mục | Thư viện | Giấy phép | Dùng ở |
|---|---|---|---|
| `three-0.169.0/` | three.js 0.169.0 (`three.module.min.js`, `OrbitControls.js`) | MIT | Văn phòng 3D (`/office`) |
