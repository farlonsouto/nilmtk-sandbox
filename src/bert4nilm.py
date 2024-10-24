import tensorflow as tf
from tensorflow.keras import layers, Model, regularizers


class LearnedL2NormPooling(layers.Layer):
    def __init__(self, pool_size=2, **kwargs):
        super(LearnedL2NormPooling, self).__init__(**kwargs)
        self.pool_size = pool_size
        self.weight = None

    def build(self, input_shape):
        self.weight = self.add_weight(
            name='l2_norm_weight',
            shape=(1, 1, input_shape[-1]),
            initializer='ones',
            trainable=True
        )

    def call(self, inputs, **kwargs):
        squared_inputs = tf.square(inputs)
        pooled = tf.nn.avg_pool2d(
            squared_inputs,
            ksize=[1, self.pool_size, 1, 1],
            strides=[1, self.pool_size, 1, 1],
            padding='VALID'
        )
        weighted_pooled = pooled * self.weight
        return tf.sqrt(weighted_pooled)


class BERT4NILM(Model):
    def __init__(self, wandb_config):
        super(BERT4NILM, self).__init__()
        self.args = wandb_config

        self.original_len = wandb_config.window_size
        self.latent_len = int(self.original_len / 2)
        self.dropout_rate = wandb_config.dropout
        self.batch_size = wandb_config.batch_size
        self.hidden = wandb_config.head_size
        self.heads = wandb_config.num_heads
        self.n_layers = wandb_config.n_layers
        self.output_size = wandb_config.output_size
        self.masking_portion = wandb_config.masking_portion
        self.conv_kernel_size = wandb_config.conv_kernel_size
        self.deconv_kernel_size = wandb_config.deconv_kernel_size
        self.ff_dim = wandb_config.ff_dim
        self.layer_norm_epsilon = wandb_config.layer_norm_epsilon
        self.kernel_initializer = wandb_config.kernel_initializer
        self.bias_initializer = wandb_config.bias_initializer
        self.kernel_regularizer = self.get_regularizer(wandb_config.kernel_regularizer)
        self.bias_regularizer = self.get_regularizer(wandb_config.bias_regularizer)

        # Convolution and pooling layers
        self.conv = layers.Conv1D(
            filters=self.hidden,
            kernel_size=self.conv_kernel_size,
            padding='same',
            activation='relu',
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )
        self.pool = LearnedL2NormPooling()

        # Positional embedding (learnable)
        self.position = layers.Embedding(
            input_dim=self.latent_len,
            output_dim=self.hidden,
            embeddings_initializer=self.kernel_initializer
        )

        # Dropout layer
        self.dropout = layers.Dropout(self.dropout_rate)

        # Transformer Encoder blocks
        self.transformer_blocks = [
            self.build_transformer_block() for _ in range(self.n_layers)
        ]

        # Deconvolution (Conv1DTranspose), and dense layers for final prediction
        self.deconv = layers.Conv1DTranspose(
            filters=self.hidden,
            kernel_size=self.deconv_kernel_size,
            strides=2,
            padding='same',
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )
        self.linear1 = layers.Dense(
            128,
            activation='tanh',
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )
        self.linear2 = layers.Dense(
            self.output_size,
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )

    @staticmethod
    def get_regularizer(regularizer_config):
        if regularizer_config == 'l1':
            return regularizers.l1(l=0.01)
        elif regularizer_config == 'l2':
            return regularizers.l2(l=0.01)
        elif regularizer_config == 'l1_l2':
            return regularizers.l1_l2(l1=0.01, l2=0.01)
        else:
            return None

    def build_transformer_block(self):
        inputs = layers.Input(shape=(None, self.hidden))

        # Multi-head attention layer
        attn_output = layers.MultiHeadAttention(
            num_heads=self.heads,
            key_dim=self.hidden // self.heads
        )(inputs, inputs)
        attn_output = layers.Dropout(self.dropout_rate)(attn_output)
        attn_output = layers.Add()([inputs, attn_output])
        attn_output = layers.LayerNormalization(epsilon=self.layer_norm_epsilon)(attn_output)

        # Feed-forward network
        ff_output = layers.Dense(
            self.ff_dim,
            activation='gelu',
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )(attn_output)
        ff_output = layers.Dense(
            self.hidden,
            kernel_initializer=self.kernel_initializer,
            bias_initializer=self.bias_initializer,
            kernel_regularizer=self.kernel_regularizer,
            bias_regularizer=self.bias_regularizer
        )(ff_output)
        ff_output = layers.Dropout(self.dropout_rate)(ff_output)
        ff_output = layers.Add()([attn_output, ff_output])
        ff_output = layers.LayerNormalization(epsilon=self.layer_norm_epsilon)(ff_output)

        return Model(inputs, ff_output)

    def call(self, inputs, training=None, mask=None):
        x_token = self.pool(self.conv(tf.expand_dims(inputs, axis=-1)))

        positions = tf.range(start=0, limit=tf.shape(x_token)[1], delta=1)
        embedding = x_token + self.position(positions)

        if training:
            mask = tf.random.uniform(shape=tf.shape(embedding)[:2]) < self.masking_portion
            embedding = tf.where(mask[:, :, tf.newaxis], 0.0, embedding)

        x = self.dropout(embedding, training=training)

        for transformer in self.transformer_blocks:
            x = transformer(x, training=training)

        x = self.deconv(x)
        x = self.linear1(x)
        return self.linear2(x)
