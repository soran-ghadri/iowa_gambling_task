import os
from typing import Tuple

import numpy as np
import tensorflow as tf
from einops import rearrange, reduce
import matplotlib.pyplot as plt

os.environ["CUDA_VISIBLE_DEVICES"] = "-1"


def masked_softmax(X, valid_lens):
    """Perform softmax operation by masking elements on the last axis."""

    # X: 3D tensor, valid_lens: 1D or 2D tensor
    def _sequence_mask(X, valid_len, value=0):
        maxlen = X.shape[1]
        mask = tf.range(start=0, limit=maxlen, dtype=tf.float32)[None, :] < tf.cast(
            valid_len[:, None], dtype=tf.float32
        )

        if len(X.shape) == 3:
            return tf.where(tf.expand_dims(mask, axis=-1), X, value)
        else:
            return tf.where(mask, X, value)

    if valid_lens is None:
        return tf.nn.softmax(X, axis=-1)
    else:
        shape = X.shape
        if len(valid_lens.shape) == 1:
            valid_lens = tf.repeat(valid_lens, repeats=shape[1])

        else:
            valid_lens = tf.reshape(valid_lens, shape=-1)
        # On the last axis, replace masked elements with a very large negative
        # value, whose exponentiation outputs 0
        X = _sequence_mask(tf.reshape(X, shape=(-1, shape[-1])), valid_lens, value=-1e6)
        return tf.nn.softmax(tf.reshape(X, shape=shape), axis=-1)


class DotProductAttention(tf.keras.layers.Layer):
    """Scaled dot product attention."""

    def __init__(self, dropout, num_heads=8):
        super().__init__()
        self.dropout = tf.keras.layers.Dropout(dropout)
        self.num_heads = num_heads  # To be covered later

    # Shape of queries: (batch_size, no. of queries, d)
    # Shape of keys: (batch_size, no. of key-value pairs, d)
    # Shape of values: (batch_size, no. of key-value pairs, value dimension)
    # Shape of valid_lens: (batch_size,) or (batch_size, no. of queries)
    def call(self, queries, keys, values, valid_lens=None, window_mask=None, **kwargs):
        d = queries.shape[-1]
        scores = tf.matmul(queries, keys, transpose_b=True) / tf.math.sqrt(
            tf.cast(d, dtype=tf.float32)
        )
        if window_mask is not None:  # To be covered later
            num_windows = window_mask.shape[0]
            n, num_queries, num_kv_pairs = scores.shape
            # Shape of window_mask: (num_windows, no. of queries,
            # no. of key-value pairs)
            scores = tf.reshape(
                scores,
                (
                    n // (num_windows * self.num_heads),
                    num_windows,
                    self.num_heads,
                    num_queries,
                    num_kv_pairs,
                ),
            ) + tf.expand_dims(tf.expand_dims(window_mask, 1), 0)
            scores = tf.reshape(scores, (n, num_queries, num_kv_pairs))
        self.attention_weights = masked_softmax(scores, valid_lens)
        return tf.matmul(self.dropout(self.attention_weights, **kwargs), values)


class MultiHeadAttention(tf.keras.layers.Layer):
    """Multi-head attention."""

    def __init__(
        self,
        key_size,
        query_size,
        value_size,
        num_hiddens,
        num_heads,
        dropout,
        bias=False,
        **kwargs,
    ):
        super(MultiHeadAttention, self).__init__()
        self.num_heads = num_heads

        self.attention = DotProductAttention(dropout, num_heads)
        self.W_q = tf.keras.layers.Dense(num_hiddens, use_bias=bias)
        self.W_k = tf.keras.layers.Dense(num_hiddens, use_bias=bias)
        self.W_v = tf.keras.layers.Dense(num_hiddens, use_bias=bias)
        self.W_o = tf.keras.layers.Dense(num_hiddens, use_bias=bias)

    def transpose_qkv(self, X: tf.Tensor) -> tf.Tensor:
        """Transpose tensors for parallel computation of attention heads.

        First transposition produces a tensor of shape X: (batch_size, num_heads, no. of queries or key-value pairs,
        num_hiddens / num_heads).
        Next it is rearranged to a new order (batch_size * num_heads, no. of queries or key-value pairs,
        num_hiddens / num_heads) which is then passed to the last rearrangement and returned.

        Parameters
        ----------
        X : Shape (batch_size, no. of queries or key-value pairs, num_hiddens).

        Returns
        -------
        X : Transposed tensor of shape ((batch_size * num_heads, no. of queries or key-value pairs,
        num_hiddens / num_heads)
                    hape of output X: (batch_size, no. of queries or key-value pairs, num_heads, num_hiddens / num_heads)
        """

        # X = tf.reshape(X, shape=(X.shape[0], X.shape[1], self.num_heads, -1))
        X = rearrange(X, "n h (heads hidden) -> n h heads hidden", heads=self.num_heads)
        print("X reshaped: ", X.shape)
        # X = tf.transpose(X, perm=(0, 2, 1, 3))
        X = rearrange(X, "b d1 d2 d3 -> b d2 d1 d3")
        print("X transposed: ", X.shape)
        # return tf.reshape(X, shape=(-1, X.shape[2], X.shape[3]))
        X = rearrange(X, "b d1 d2 d3 -> (b d1) d2 d3")
        print("X reshaped2: ", X.shape)
        return X

    def inverse_transpose_qkv(self, X):
        """Reverse the operation of transpose_qkv."""
        X = tf.reshape(X, shape=(-1, self.num_heads, X.shape[1], X.shape[2]))
        X = tf.transpose(X, perm=(0, 2, 1, 3))
        return tf.reshape(X, shape=(X.shape[0], X.shape[1], -1))

    def call(self, queries, values, keys, valid_lens, window_mask=None, **kwargs):
        # Shape of queries, keys, or values:
        # (batch_size, no. of queries or key-value pairs, num_hiddens)
        # Shape of valid_lens: (batch_size,) or (batch_size, no. of queries)
        # After transposing, shape of output queries, keys, or values:
        # (batch_size * num_heads, no. of queries or key-value pairs,
        # num_hiddens / num_heads)

        print("wq(queries): ", self.W_q(queries).shape)
        print("queries: ", queries.shape)
        queries = self.transpose_qkv(self.W_q(queries))
        print("keys: ", keys.shape)
        keys = self.transpose_qkv(self.W_k(keys))
        print("values: ", values.shape)
        values = self.transpose_qkv(self.W_v(values))

        if valid_lens is not None:
            # On axis 0, copy the first item (scalar or vector) for num_heads
            # times, then copy the next item, and so on
            valid_lens = tf.repeat(valid_lens, repeats=self.num_heads, axis=0)

        # Shape of output: (batch_size * num_heads, no. of queries,
        # num_hiddens / num_heads)
        output = self.attention(
            queries, keys, values, valid_lens, window_mask, **kwargs
        )

        # Shape of output_concat: (batch_size, no. of queries, num_hiddens)
        output_concat = self.inverse_transpose_qkv(output)
        return self.W_o(output_concat)


class PositionalEncoding(tf.keras.layers.Layer):
    def __init__(self, num_hiddens, dropout, max_len=1000):
        super().__init__()
        self.dropout = tf.keras.layers.Dropout(dropout)
        # Create a long enough P
        self.P = np.zeros((1, max_len, num_hiddens))
        X = np.arange(max_len, dtype=np.float32).reshape(-1, 1) / np.power(
            10000, np.arange(0, num_hiddens, 2, dtype=np.float32) / num_hiddens
        )
        self.P[:, :, 0::2] = np.sin(X)
        self.P[:, :, 1::2] = np.cos(X)

    def call(self, X, **kwargs):
        X = X + self.P[:, : X.shape[1], :]
        return self.dropout(X, **kwargs)


class Plot:
    def plot_pe(self, y: Tuple[int, np.array], encodings, num_steps, label):
        fig, ax = plt.subplots(figsize=(6, 2.5))
        lines = []

        for idx in range(y[0]):
            line = ax.plot(
                np.arange(num_steps),
                encodings[0, :, idx].T,
                dashes=[2, int(idx / 2), idx, 2],
                label=label,
            )
            lines.append(line)
        ax.legend()
        # plt.rcParams["figure.figsize"] = (20, 3)
        plt.show()


encoding_dim, num_steps = 32, 60
pos_encoding = PositionalEncoding(encoding_dim, 0)
X = pos_encoding(tf.zeros((1, num_steps, encoding_dim)), training=False)
P = pos_encoding.P[:, : X.shape[1], :]

print(X.shape)
print(P[0, :, 6:10].T.shape)
print(np.arange(num_steps).shape)
plotter = Plot()
label = "row position"

fig, ax = plt.subplots(figsize=(6, 2.5))
ax.plot(
    np.arange(num_steps),
    P[0, :, 6].T,
    dashes=[2, 2, 10, 2],
    label=label,
)
ax.plot(
    np.arange(num_steps),
    P[0, :, 8].T,
    dashes=[2, 2, 10, 2],
    label=label,
)
ax.legend()
plt.show()
# plotter.plot_pe(P[0, :, 6:10].T.shape, P, num_steps, label)
