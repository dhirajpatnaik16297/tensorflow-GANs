#-*- coding: utf-8 -*-
from __future__ import division
import os
import time
import tensorflow as tf
import numpy as np

from ops import *
from utils import *

import prior_factory as prior

class VAE(object):
    def __init__(self, sess, epoch, batch_size, z_dim, dataset_name, checkpoint_dir, result_dir, log_dir):
        self.sess = sess
        self.dataset_name = dataset_name
        self.checkpoint_dir = checkpoint_dir
        self.result_dir = result_dir
        self.log_dir = log_dir
        self.epoch = epoch
        self.batch_size = batch_size
        self.model_name = "VAE"     # name for checkpoint

        if dataset_name == 'mnist' or dataset_name == 'fashion-mnist':
            # parameters
            self.input_height = 28
            self.input_width = 28
            self.output_height = 28
            self.output_width = 28

            self.z_dim = z_dim         # dimension of noise-vector
            self.c_dim = 1

            # train
            self.learning_rate = 0.0002
            self.beta1 = 0.5

            # test
            self.sample_num = 64  # number of generated images to be saved

            # load mnist
            self.data_X, self.data_y = load_mnist(self.dataset_name)

            # get number of batches for a single epoch
            self.num_batches = len(self.data_X) // self.batch_size
        else:
            raise NotImplementedError

    # Gaussian Encoder 高斯编码器
    def encoder(self, x, is_training=True, reuse=False):
        # Network Architecture is exactly same as in infoGAN (https://arxiv.org/abs/1606.03657)
        # Architecture : (64)4c2s-(128)4c2s_BL-FC1024_BL-FC62*4
        with tf.variable_scope("encoder", reuse=reuse):
            # 经过这一步卷积后，(64,28,28,1)-->(64,14,14,64) 具体的计算为(28-2)/2+1
            net = lrelu(conv2d(x, 64, 4, 4, 2, 2, name='en_conv1'))
            # 经过这一步卷积后，(64,14,14,64)-->(64,7,7,128) 具体的计算为(14-2)/2+1
            net = lrelu(bn(conv2d(net, 128, 4, 4, 2, 2, name='en_conv2'), is_training=is_training, scope='en_bn2'))
            # 经过数组重构后，(64,7,7,128)-->(64,6272)
            net = tf.reshape(net, [self.batch_size, -1])
            # 经过线性处理后将矩阵，(64,6272)-->(64,1024)
            net = lrelu(bn(linear(net, 1024, scope='en_fc3'), is_training=is_training, scope='en_bn3'))
            # 经过线性处理后 (64,1024)-->(64,124)
            gaussian_params = linear(net, 2 * self.z_dim, scope='en_fc4')

            # The mean parameter is unconstrained 将输出分为两块，z_mean和z_log_var,就是高斯分布的均值和标准差
            # 分出的前（64,62）代表均值， 后（64,62）处理后代表标准差
            mean = gaussian_params[:, :self.z_dim]
            # The standard deviation must be positive. Parametrize with a softplus and
            # add a small epsilon for numerical stability
            # 标准差必须是正值。 用softplus参数化并为数值稳定性添加一个小的epsilon, softplus为y=log(1+ex)
            stddev = 1e-6 + tf.nn.softplus(gaussian_params[:, self.z_dim:])

        return mean, stddev

    # Bernoulli decoder 伯努利解码器
    def decoder(self, z, is_training=True, reuse=False):
        # Network Architecture is exactly same as in infoGAN (https://arxiv.org/abs/1606.03657)
        # Architecture : FC1024_BR-FC7x7x128_BR-(64)4dc2s_BR-(1)4dc2s_S
        with tf.variable_scope("decoder", reuse=reuse):
            # 经过线性处理后将矩阵，(64,62)-->(64,1024)
            net = tf.nn.relu(bn(linear(z, 1024, scope='de_fc1'), is_training=is_training, scope='de_bn1'))
            # 经过线性处理后将矩阵，(64,1024)-->(64,6272), 6272=128*7*7
            net = tf.nn.relu(bn(linear(net, 128 * 7 * 7, scope='de_fc2'), is_training=is_training, scope='de_bn2'))
            # 经过重构后，形状变为(64,7,7,128)
            net = tf.reshape(net, [self.batch_size, 7, 7, 128])
            # 经过deconv2d,(64,7,7,128)-->(64,14,14,128)
            net = tf.nn.relu(
                bn(deconv2d(net, [self.batch_size, 14, 14, 64], 4, 4, 2, 2, name='de_dc3'), is_training=is_training,
                   scope='de_bn3'))

            # 经过deconv2d,(64,14,14,128)-->(64,28,28,1),将值处理用sigmoid处理至（0,1）之间,`y = 1 / (1 + exp(-x))`
            out = tf.nn.sigmoid(deconv2d(net, [self.batch_size, 28, 28, 1], 4, 4, 2, 2, name='de_dc4'))
            return out

    # 建立VAE模型，此函数非常重要
    def build_model(self):
        # some parameters
        image_dims = [self.input_height, self.input_width, self.c_dim]
        bs = self.batch_size

        """ Graph Input """
        # images
        self.inputs = tf.placeholder(tf.float32, [bs] + image_dims, name='real_images')

        # noises
        self.z = tf.placeholder(tf.float32, [bs, self.z_dim], name='z')

        """ Loss Function """
        # encoding
        self.mu, sigma = self.encoder(self.inputs, is_training=True, reuse=False)        

        # sampling by re-parameterization technique
        # tf.random_normal 从正态分布输出随机值, sigma乘上随机值后将服从正态分布，抽空博客仔细说一下
        z = self.mu + sigma * tf.random_normal(tf.shape(self.mu), 0, 1, dtype=tf.float32)

        # decoding
        out = self.decoder(z, is_training=True, reuse=False)
        self.out = tf.clip_by_value(out, 1e-8, 1 - 1e-8)

        # loss
        marginal_likelihood = tf.reduce_sum(self.inputs * tf.log(self.out) + (1 - self.inputs) * tf.log(1 - self.out),
                                            [1, 2])
        # 在我写的Word中有对KL实现的讲解
        KL_divergence = 0.5 * tf.reduce_sum(tf.square(self.mu) + tf.square(sigma) - tf.log(1e-8 + tf.square(sigma)) - 1,
                                            [1])

        self.neg_loglikelihood = -tf.reduce_mean(marginal_likelihood)
        self.KL_divergence = tf.reduce_mean(KL_divergence)

        ELBO = -self.neg_loglikelihood - self.KL_divergence

        self.loss = -ELBO

        """ Training """
        # optimizers 训练优化loss函数，依旧采用Adam优化器
        t_vars = tf.trainable_variables()
        with tf.control_dependencies(tf.get_collection(tf.GraphKeys.UPDATE_OPS)):
            self.optim = tf.train.AdamOptimizer(self.learning_rate*5, beta1=self.beta1) \
                      .minimize(self.loss, var_list=t_vars)

        """" Testing """
        # for test 由噪声生成一张图片
        self.fake_images = self.decoder(self.z, is_training=False, reuse=True)

        """ Summary """
        nll_sum = tf.summary.scalar("nll", self.neg_loglikelihood)
        kl_sum = tf.summary.scalar("kl", self.KL_divergence)
        loss_sum = tf.summary.scalar("loss", self.loss)

        # final summary operations
        self.merged_summary_op = tf.summary.merge_all()

    def train(self):

        # initialize all variables
        tf.global_variables_initializer().run()

        # graph inputs for visualize training results
        # 创建高斯分布z，均值为0，方差为1
        self.sample_z = prior.gaussian(self.batch_size, self.z_dim)

        # saver to save model
        self.saver = tf.train.Saver()

        # summary writer
        self.writer = tf.summary.FileWriter(self.log_dir + '/' + self.model_name, self.sess.graph)

        # restore check-point if it exits
        could_load, checkpoint_counter = self.load(self.checkpoint_dir)
        if could_load:
            start_epoch = (int)(checkpoint_counter / self.num_batches)
            start_batch_id = checkpoint_counter - start_epoch * self.num_batches
            counter = checkpoint_counter
            print(" [*] Load SUCCESS")
        else:
            start_epoch = 0
            start_batch_id = 0
            counter = 1
            print(" [!] Load failed...")

        # loop for epoch
        start_time = time.time()
        for epoch in range(start_epoch, self.epoch):

            # get batch data
            for idx in range(start_batch_id, self.num_batches):
                batch_images = self.data_X[idx*self.batch_size:(idx+1)*self.batch_size]
                # 创建高斯分布z，均值为0，方差为1
                batch_z = prior.gaussian(self.batch_size, self.z_dim)

                # update autoencoder
                _, summary_str, loss, nll_loss, kl_loss = self.sess.run([self.optim, self.merged_summary_op, self.loss,
                                                                         self.neg_loglikelihood, self.KL_divergence],
                                               feed_dict={self.inputs: batch_images, self.z: batch_z})
                self.writer.add_summary(summary_str, counter)

                # display training status
                counter += 1
                if np.mod(counter, 50) == 0:
                    print("Epoch: [%2d] [%4d/%4d] time: %4.4f, loss: %.8f, nll: %.8f, kl: %.8f" \
                          % (epoch, idx, self.num_batches, time.time() - start_time, loss, nll_loss, kl_loss))

                # save training results for every 300 steps 训练300步保存一张生成图片
                if np.mod(counter, 300) == 0:
                    samples = self.sess.run(self.fake_images,
                                            feed_dict={self.z: self.sample_z})

                    tot_num_samples = min(self.sample_num, self.batch_size)
                    manifold_h = int(np.floor(np.sqrt(tot_num_samples)))
                    manifold_w = int(np.floor(np.sqrt(tot_num_samples)))
                    save_images(samples[:manifold_h * manifold_w, :, :, :], [manifold_h, manifold_w],
                                './' + check_folder(self.result_dir + '/' + self.model_dir) + '/' + self.model_name +
                                '_train_{:02d}_{:04d}.png'.format(epoch, idx))

            # After an epoch, start_batch_id is set to zero
            # non-zero value is only for the first epoch after loading pre-trained model
            start_batch_id = 0

            # save model
            self.save(self.checkpoint_dir, counter)

            # show temporal results
            self.visualize_results(epoch)

        # save model for final step
        self.save(self.checkpoint_dir, counter)

    # 用于可视化epoch后输出图片
    def visualize_results(self, epoch):
        tot_num_samples = min(self.sample_num, self.batch_size)
        image_frame_dim = int(np.floor(np.sqrt(tot_num_samples)))

        """ random condition, random noise """

        z_sample = prior.gaussian(self.batch_size, self.z_dim)

        samples = self.sess.run(self.fake_images, feed_dict={self.z: z_sample})

        save_images(samples[:image_frame_dim * image_frame_dim, :, :, :], [image_frame_dim, image_frame_dim],
                    check_folder(
                        self.result_dir + '/' + self.model_dir) + '/' + self.model_name + '_epoch%03d' % epoch +
                    '_test_all_classes.png')

        # 对于噪声向量维度为2的时候，模型可以理解为一个分类状况，此时可以用散点图表示出来各类之间的分类情况
        # 需要注意的是此时的z_dim=2
        """ learned manifold """
        if self.z_dim == 2:
            assert self.z_dim == 2

            z_tot = None
            id_tot = None
            for idx in range(0, 100):
                #randomly sampling
                id = np.random.randint(0, self.num_batches)
                batch_images = self.data_X[id * self.batch_size:(id + 1) * self.batch_size]
                # 用于记录图片对应的标签用于类别显示
                batch_labels = self.data_y[id * self.batch_size:(id + 1) * self.batch_size]

                z = self.sess.run(self.mu, feed_dict={self.inputs: batch_images})

                if idx == 0:
                    z_tot = z
                    id_tot = batch_labels
                else:
                    z_tot = np.concatenate((z_tot, z), axis=0)
                    id_tot = np.concatenate((id_tot, batch_labels), axis=0)

            save_scattered_image(z_tot, id_tot, -4, 4, name=check_folder(
                self.result_dir + '/' + self.model_dir) + '/' + self.model_name + '_epoch%03d' % epoch + '_learned_manifold.png')

    @property
    # 加载创建固定模型下的路径，本处为VAE下的训练
    def model_dir(self):
        return "{}_{}_{}_{}".format(
            self.model_name, self.dataset_name,
            self.batch_size, self.z_dim)

    # 本函数的目的是在于保存训练模型后的checkpoint
    def save(self, checkpoint_dir, step):
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir, self.model_name)

        if not os.path.exists(checkpoint_dir):
            os.makedirs(checkpoint_dir)

        self.saver.save(self.sess,os.path.join(checkpoint_dir, self.model_name+'.model'), global_step=step)

    # 本函数的意义在于读取训练好的模型参数的checkpoint
    def load(self, checkpoint_dir):
        import re
        print(" [*] Reading checkpoints...")
        checkpoint_dir = os.path.join(checkpoint_dir, self.model_dir, self.model_name)

        ckpt = tf.train.get_checkpoint_state(checkpoint_dir)
        if ckpt and ckpt.model_checkpoint_path:
            ckpt_name = os.path.basename(ckpt.model_checkpoint_path)
            self.saver.restore(self.sess, os.path.join(checkpoint_dir, ckpt_name))
            counter = int(next(re.finditer("(\d+)(?!.*\d)",ckpt_name)).group(0))
            print(" [*] Success to read {}".format(ckpt_name))
            return True, counter
        else:
            print(" [*] Failed to find a checkpoint")
            return False, 0