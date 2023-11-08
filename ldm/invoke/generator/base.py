'''
Base class for ldm.invoke.generator.*
including img2img, txt2img, and inpaint
'''
import torch
import numpy as  np
import random
import os
import traceback
from tqdm import tqdm, trange
from PIL import Image, ImageFilter
from einops import rearrange, repeat
from pytorch_lightning import seed_everything
from ldm.invoke.devices import choose_autocast
from ldm.util import rand_perlin_2d

downsampling = 8
CAUTION_IMG = 'assets/caution.png'

class Generator():
    def __init__(self, model, precision):
        self.model = model
        self.precision = precision
        self.seed = None
        self.latent_channels = model.channels
        self.downsampling_factor = downsampling   # BUG: should come from model or config
        self.safety_checker = None
        self.perlin = 0.0
        self.threshold = 0
        self.variation_amount = 0
        self.with_variations = []
        self.use_mps_noise = False
        self.free_gpu_mem = None

    # this is going to be overridden in img2img.py, txt2img.py and inpaint.py
    def get_make_image(self,prompt,**kwargs):
        """
        Returns a function returning an image derived from the prompt and the initial image
        Return value depends on the seed at the time you call it
        """
        raise NotImplementedError("image_iterator() must be implemented in a descendent class")

    def set_variation(self, seed, variation_amount, with_variations):
        self.seed             = seed
        self.variation_amount = variation_amount
        self.with_variations  = with_variations

    def generate(self,prompt,init_image,width,height,sampler, iterations=1,seed=None,
                 image_callback=None, step_callback=None, threshold=0.0, perlin=0.0,
                 safety_checker:dict=None,
                 **kwargs):
        scope = choose_autocast(self.precision)
        self.safety_checker = safety_checker
        make_image = self.get_make_image(
            prompt,
            sampler = sampler,
            init_image    = init_image,
            width         = width,
            height        = height,
            step_callback = step_callback,
            threshold     = threshold,
            perlin        = perlin,
            **kwargs
        )
        results             = []
        seed                = seed if seed is not None and seed >= 0 else self.new_seed()
        first_seed          = seed
        seed, initial_noise = self.generate_initial_noise(seed, width, height)

        # There used to be an additional self.model.ema_scope() here, but it breaks
        # the inpaint-1.5 model. Not sure what it did.... ?
        with scope(self.model.device.type):
            for _ in trange(iterations, desc='Generating'):
                x_T = None
                if self.variation_amount > 0:
                    seed_everything(seed)
                    target_noise = self.get_noise(width,height)
                    x_T = self.slerp(self.variation_amount, initial_noise, target_noise)
                elif initial_noise is not None:
                    # i.e. we specified particular variations
                    x_T = initial_noise
                else:
                    seed_everything(seed)
                    try:
                        x_T = self.get_noise(width,height)
                    except:
                        print('** An error occurred while getting initial noise **')
                        print(traceback.format_exc())

                image = make_image(x_T)

                if self.safety_checker is not None:
                    image = self.safety_check(image)

                results.append([image, seed])

                if image_callback is not None:
                    image_callback(image, seed, first_seed=first_seed)

                seed = self.new_seed()

        return results
    
    def sample_to_image(self,samples)->Image.Image:
        """
        Given samples returned from a sampler, converts
        it into a PIL Image
        """
        x_samples = self.model.decode_first_stage(samples)
        x_samples = torch.clamp((x_samples + 1.0) / 2.0, min=0.0, max=1.0)
        if len(x_samples) != 1:
            raise Exception(
                f'>> expected to get a single image, but got {len(x_samples)}')
        x_sample = 255.0 * rearrange(
            x_samples[0].cpu().numpy(), 'c h w -> h w c'
        )
        return Image.fromarray(x_sample.astype(np.uint8))

        # write an approximate RGB image from latent samples for a single step to PNG

    def sample_to_lowres_estimated_image(self,samples):
        # origingally adapted from code by @erucipe and @keturn here:
        # https://discuss.huggingface.co/t/decoding-latents-to-rgb-without-upscaling/23204/7

        # these updated numbers for v1.5 are from @torridgristle
        v1_5_latent_rgb_factors = torch.tensor([
            #    R        G        B
            [ 0.3444,  0.1385,  0.0670], # L1
            [ 0.1247,  0.4027,  0.1494], # L2
            [-0.3192,  0.2513,  0.2103], # L3
            [-0.1307, -0.1874, -0.7445]  # L4
        ], dtype=samples.dtype, device=samples.device)

        latent_image = samples[0].permute(1, 2, 0) @ v1_5_latent_rgb_factors
        latents_ubyte = (((latent_image + 1) / 2)
                         .clamp(0, 1)  # change scale from -1..1 to 0..1
                         .mul(0xFF)  # to 0..255
                         .byte()).cpu()

        return Image.fromarray(latents_ubyte.numpy())

    def generate_initial_noise(self, seed, width, height):
        initial_noise = None
        if self.variation_amount > 0 or len(self.with_variations) > 0:
            # use fixed initial noise plus random noise per iteration
            seed_everything(seed)
            initial_noise = self.get_noise(width,height)
            for v_seed, v_weight in self.with_variations:
                seed = v_seed
                seed_everything(seed)
                next_noise = self.get_noise(width,height)
                initial_noise = self.slerp(v_weight, initial_noise, next_noise)
            if self.variation_amount > 0:
                random.seed() # reset RNG to an actually random state, so we can get a random seed for variations
                seed = random.randrange(0,np.iinfo(np.uint32).max)
            return (seed, initial_noise)
        else:
            return (seed, None)

    # returns a tensor filled with random numbers from a normal distribution
    def get_noise(self,width,height):
        """
        Returns a tensor filled with random numbers, either form a normal distribution
        (txt2img) or from the latent image (img2img, inpaint)
        """
        raise NotImplementedError("get_noise() must be implemented in a descendent class")
    
    def get_perlin_noise(self,width,height):
        fixdevice = 'cpu' if (self.model.device.type == 'mps') else self.model.device
        return torch.stack([rand_perlin_2d((height, width), (8, 8), device = self.model.device).to(fixdevice) for _ in range(self.latent_channels)], dim=0).to(self.model.device)
    
    def new_seed(self):
        self.seed = random.randrange(0, np.iinfo(np.uint32).max)
        return self.seed

    def slerp(self, t, v0, v1, DOT_THRESHOLD=0.9995):
        '''
        Spherical linear interpolation
        Args:
            t (float/np.ndarray): Float value between 0.0 and 1.0
            v0 (np.ndarray): Starting vector
            v1 (np.ndarray): Final vector
            DOT_THRESHOLD (float): Threshold for considering the two vectors as
                                colineal. Not recommended to alter this.
        Returns:
            v2 (np.ndarray): Interpolation vector between v0 and v1
        '''
        inputs_are_torch = False
        if not isinstance(v0, np.ndarray):
            inputs_are_torch = True
            v0 = v0.detach().cpu().numpy()
        if not isinstance(v1, np.ndarray):
            inputs_are_torch = True
            v1 = v1.detach().cpu().numpy()

        dot = np.sum(v0 * v1 / (np.linalg.norm(v0) * np.linalg.norm(v1)))
        if np.abs(dot) > DOT_THRESHOLD:
            v2 = (1 - t) * v0 + t * v1
        else:
            theta_0 = np.arccos(dot)
            sin_theta_0 = np.sin(theta_0)
            theta_t = theta_0 * t
            sin_theta_t = np.sin(theta_t)
            s0 = np.sin(theta_0 - theta_t) / sin_theta_0
            s1 = sin_theta_t / sin_theta_0
            v2 = s0 * v0 + s1 * v1

        if inputs_are_torch:
            v2 = torch.from_numpy(v2).to(self.model.device)

        return v2

    def safety_check(self,image:Image.Image):
        '''
        If the CompViz safety checker flags an NSFW image, we
        blur it out.
        '''
        import diffusers

        checker = self.safety_checker['checker']
        extractor = self.safety_checker['extractor']
        features = extractor([image], return_tensors="pt")
        features.to(self.model.device)

        # unfortunately checker requires the numpy version, so we have to convert back
        x_image = np.array(image).astype(np.float32) / 255.0
        x_image = x_image[None].transpose(0, 3, 1, 2)

        diffusers.logging.set_verbosity_error()
        checked_image, has_nsfw_concept = checker(images=x_image, clip_input=features.pixel_values)
        if has_nsfw_concept[0]:
            print('** An image with potential non-safe content has been detected. A blurred image will be returned. **')
            return self.blur(image)
        else:
            return image

    def blur(self,input):
        blurry = input.filter(filter=ImageFilter.GaussianBlur(radius=32))
        try:
            caution = Image.open(CAUTION_IMG)
            caution = caution.resize((caution.width // 2, caution.height //2))
            blurry.paste(caution,(0,0),caution)
        except FileNotFoundError:
            pass
        return blurry

    # this is a handy routine for debugging use. Given a generated sample,
    # convert it into a PNG image and store it at the indicated path
    def save_sample(self, sample, filepath):
        image = self.sample_to_image(sample)
        dirname = os.path.dirname(filepath) or '.'
        if not os.path.exists(dirname):
            print(f'** creating directory {dirname}')
            os.makedirs(dirname, exist_ok=True)
        image.save(filepath,'PNG')

        
