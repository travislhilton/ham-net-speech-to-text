"""LZHUF compressed size, as Winlink computes it for a message (Okumura's LZHUF with Winlink's parameters:
2 KB window, look-ahead 60; the transfer adds a 2-byte CRC + 4-byte length). Only the output LENGTH is used
(Winlink Express's message list records it); the bits themselves are counted, never sent."""
N, F, THRESHOLD = 2048, 60, 2
NIL = N
N_CHAR = 256 - THRESHOLD + F
T = N_CHAR * 2 - 1
R = T - 1
MAX_FREQ = 0x8000
P_LEN = [3] + [4] * 3 + [5] * 8 + [6] * 12 + [7] * 24 + [8] * 16


class _Enc:
    def __init__(self):
        self.lson = [NIL] * (N + 1); self.rson = [NIL] * (N + 257); self.dad = [NIL] * (N + 1)
        self.buf = bytearray(N + F - 1)
        self.freq = [0] * (T + 1); self.prnt = [0] * (T + N_CHAR); self.son = [0] * T
        self.bits = 0; self.mlen = 0; self.mpos = 0
        for i in range(N_CHAR):
            self.freq[i] = 1; self.son[i] = i + T; self.prnt[i + T] = i
        i, j = 0, N_CHAR
        while j <= R:
            self.freq[j] = self.freq[i] + self.freq[i + 1]; self.son[j] = i
            self.prnt[i] = self.prnt[i + 1] = j; i += 2; j += 1
        self.freq[T] = 0xFFFF; self.prnt[R] = 0

    def insert(self, r):
        buf, lson, rson, dad = self.buf, self.lson, self.rson, self.dad
        cmp, p = 1, N + 1 + buf[r]
        rson[r] = lson[r] = NIL; self.mlen = 0
        while True:
            if cmp >= 0:
                if rson[p] != NIL: p = rson[p]
                else: rson[p] = r; dad[r] = p; return
            else:
                if lson[p] != NIL: p = lson[p]
                else: lson[p] = r; dad[r] = p; return
            i = 1
            while i < F:
                cmp = buf[r + i] - buf[p + i]
                if cmp != 0: break
                i += 1
            if i > THRESHOLD:
                if i > self.mlen:
                    self.mpos = ((r - p) & (N - 1)) - 1
                    self.mlen = i
                    if i >= F: break
                if i == self.mlen:
                    c = ((r - p) & (N - 1)) - 1
                    if c < self.mpos: self.mpos = c
        dad[r] = dad[p]; lson[r] = lson[p]; rson[r] = rson[p]
        dad[lson[p]] = r; dad[rson[p]] = r
        if rson[dad[p]] == p: rson[dad[p]] = r
        else: lson[dad[p]] = r
        dad[p] = NIL

    def delete(self, p):
        lson, rson, dad = self.lson, self.rson, self.dad
        if dad[p] == NIL: return
        if rson[p] == NIL: q = lson[p]
        elif lson[p] == NIL: q = rson[p]
        else:
            q = lson[p]
            if rson[q] != NIL:
                while rson[q] != NIL: q = rson[q]
                rson[dad[q]] = lson[q]; dad[lson[q]] = dad[q]
                lson[q] = lson[p]; dad[lson[p]] = q
            rson[q] = rson[p]; dad[rson[p]] = q
        dad[q] = dad[p]
        if rson[dad[p]] == p: rson[dad[p]] = q
        else: lson[dad[p]] = q
        dad[p] = NIL

    def reconst(self):
        freq, son, prnt = self.freq, self.son, self.prnt
        j = 0
        for i in range(T):
            if son[i] >= T:
                freq[j] = (freq[i] + 1) // 2; son[j] = son[i]; j += 1
        i, j = 0, N_CHAR
        while j < T:
            f = freq[j] = freq[i] + freq[i + 1]
            k = j - 1
            while f < freq[k]: k -= 1
            k += 1
            freq[k + 1:j + 1] = freq[k:j]; freq[k] = f
            son[k + 1:j + 1] = son[k:j]; son[k] = i
            i += 2; j += 1
        for i in range(T):
            k = son[i]
            if k >= T: prnt[k] = i
            else: prnt[k] = prnt[k + 1] = i

    def update(self, c):
        freq, son, prnt = self.freq, self.son, self.prnt
        if freq[R] == MAX_FREQ: self.reconst()
        c = prnt[c + T]
        while True:
            freq[c] += 1; k = freq[c]
            l = c + 1
            if k > freq[l]:
                l += 1
                while k > freq[l]: l += 1
                l -= 1
                freq[c] = freq[l]; freq[l] = k
                i = son[c]; prnt[i] = l
                if i < T: prnt[i + 1] = l
                j = son[l]; son[l] = i
                prnt[j] = c
                if j < T: prnt[j + 1] = c
                son[c] = j
                c = l
            c = prnt[c]
            if c == 0: break

    def char(self, c):
        j, k = 0, self.prnt[c + T]
        while True:
            j += 1; k = self.prnt[k]
            if k == R: break
        self.bits += j
        self.update(c)

    def pos(self, c):
        self.bits += P_LEN[c >> 6] + 6


def compressed_size(data: bytes, crc: bool = True) -> int:
    e = _Enc()
    if data:
        buf, n, src = e.buf, len(data), 0
        s, r = 0, N - F
        for i in range(r): buf[i] = 0x20
        ln = 0
        while ln < F and src < n:
            buf[r + ln] = data[src]; ln += 1; src += 1
        for i in range(1, F + 1): e.insert(r - i)
        e.insert(r)
        while ln > 0:
            if e.mlen > ln: e.mlen = ln
            if e.mlen <= THRESHOLD:
                e.mlen = 1; e.char(buf[r])
            else:
                e.char(255 - THRESHOLD + e.mlen); e.pos(e.mpos)
            last = e.mlen
            i = 0
            while i < last and src < n:
                c = data[src]; src += 1
                e.delete(s); buf[s] = c
                if s < F - 1: buf[s + N] = c
                s = (s + 1) & (N - 1); r = (r + 1) & (N - 1)
                e.insert(r); i += 1
            while i < last:
                i += 1
                e.delete(s); s = (s + 1) & (N - 1); r = (r + 1) & (N - 1)
                ln -= 1
                if ln: e.insert(r)
    return (e.bits + 7) // 8 + 4 + (2 if crc else 0)
